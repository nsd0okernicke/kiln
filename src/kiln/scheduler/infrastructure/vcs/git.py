"""
Git operations the wrapper LLM used to perform by following prose.

Codifies `/kiln-receive` step 4 (merge the sender's commit) and `/kiln-handoff` step 2
(squash everything since the merge anchor into one role-prefixed commit).

These shell out to real git rather than abstracting it behind an injectable runner: git is
already a hard dependency of every Kiln workspace, and tests against real temporary repos
catch the things that actually break here (conflicts, empty squashes, detached anchors)
which a fake runner would only pretend to model.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

log = logging.getLogger(__name__)

#: Guards against a hung git invocation wedging a headless scheduler with nobody watching.
DEFAULT_TIMEOUT_SEC = 120

#: Files the launcher writes, untracked, into every worktree.
#:
#: These are the scheduler's own footprint, and they are radioactive: `squash_since` runs
#: `git add -A` to capture whatever the worker produced, which also sweeps up any of these
#: that git does not ignore. The commit then carries a file that every *other* worktree
#: still holds as untracked, and that role's next merge aborts with "untracked working tree
#: files would be overwritten" — a deadlock needing manual git surgery to clear.
#:
#: Observed live: `.claude/settings.json` was committed by the specifier's squash and
#: instantly wedged the coder. `tmp/` was the same bug found earlier.
#:
#: Mirrors the launcher's required gitignore entries, duplicated on purpose so the
#: scheduler stays runnable without the launcher package on the path.
GENERATED_WORKTREE_PATHS = (
    "tmp/",
    ".claude/settings.json",
    ".mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
)

_UNTRACKED_BLOCKER = "untracked working tree files would be overwritten"


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int

    @property
    def output(self) -> str:
        """Combined output, for error reporting to a human."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


def run_git(
    args: list[str],
    cwd: str | Path,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> GitResult:
    """Run one git command, never raising on non-zero exit."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.error("git %s timed out after %ss", " ".join(args), timeout)
        return GitResult(False, "", f"git {' '.join(args)} timed out", returncode=-1)

    return GitResult(
        ok=completed.returncode == 0,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        returncode=completed.returncode,
    )


def head_commit(cwd: str | Path) -> str:
    """Current HEAD hash, or '' when the repo has no commits yet."""
    result = run_git(["rev-parse", "HEAD"], cwd)
    return result.stdout if result.ok else ""


def current_branch(cwd: str | Path) -> str:
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return result.stdout if result.ok else ""


def merge_commit(commit: str, cwd: str | Path, message: str | None = None) -> GitResult:
    """
    Merge the sender's commit. The resulting merge commit becomes the squash anchor.

    A failure here must stop the cycle before any delegation — per /kiln-receive step 4,
    "if the merge fails, stop and report the error before proceeding". Working on top of a
    conflicted tree would produce a handoff nobody can use.

    `--no-ff` is essential, not cosmetic. A fast-forward merge creates no merge commit, so
    `squash_anchor` would find none and fall back to the repository's ROOT commit — and the
    next `git reset --soft` would then collapse the entire project history into a single
    commit. Forcing a merge commit guarantees every cycle has a well-defined anchor.

    `message`, when given, replaces git's default "Merge commit '<hash>' into <branch>"
    subject — uninformative in `git log`, and identical for every merge regardless of who
    sent what. Callers build one with `role_scheduler.merge_commit_message`. Falls back to
    `--no-edit` (git's default message) when omitted, for callers with nothing to say.
    """
    args = (
        ["merge", "--no-ff", "-m", message, commit]
        if message
        else ["merge", "--no-ff", "--no-edit", commit]
    )
    result = run_git(args, cwd)
    if result.ok:
        return result

    # A merge blocked *only* by Kiln's own scaffolding is recoverable: the incoming commit
    # carries the same generated content, and the launcher rewrites these files on the next
    # launch anyway. Retry once after clearing them rather than escalating a deadlock that
    # a human could only fix with the same `rm`.
    if _clear_generated_blockers(result.output, cwd):
        result = run_git(args, cwd)
        if result.ok:
            return result

    log.error("merge of %s failed: %s", commit, result.output)
    # Leave no half-merged tree behind for the next cycle to trip over.
    run_git(["merge", "--abort"], cwd)
    return result


def squash_merge_commit(commit: str, cwd: str | Path, message: str) -> GitResult:
    """
    Merge the sender's commit as one flat commit, then record where it came from.

    For `human-in-the-loop`'s inbox specifically. That role works directly on the project's
    real, potentially-pushed branch (`@current`), not a disposable local sub-branch the way
    every scheduled role does — so a real `--no-ff` merge (`merge_commit`, above) would put
    the sender's entire commit graph on that branch's *first-parent line* on every single
    handoff, including whatever that sender had already merged in from its own senders.
    `git merge --squash` stages the diff instead, and the follow-up commit lands looking like
    one ordinary commit.

    `record_provenance` then adds the parent link back, without touching the tree. Squashing
    alone was not survivable: it is the one hop in the lap that drops ancestry, and the next
    time the work came round to the role that wrote it, git had no way to tell that one side
    was a descendant of the other and conflicted. See `record_provenance` for the full case.

    Not a substitute for `merge_commit` anywhere a scheduler role receives: `squash_anchor`
    locates its anchor via `git log --merges -1`, which requires a real merge commit to
    exist. Using this for a scheduled role's own inbound merge would remove that anchor and
    silently fall back to the repository's ROOT commit, collapsing the whole project history
    into one commit on that role's next squash.
    """
    result = _squash_attempt(commit, cwd)

    if not result.ok:
        log.error("squash-merge of %s failed: %s", commit, result.output)
        # `--squash` never sets MERGE_HEAD, so `merge --abort` has nothing to abort -- only a
        # hard reset actually clears the conflicted index/worktree it can leave behind.
        run_git(["reset", "--hard", "HEAD"], cwd)
        return result

    if not has_pending_changes(cwd):
        # Content identical to what's already there (e.g. a re-sent or no-op handoff) -- that
        # is success, not failure. Nothing to commit; reuse HEAD as the resulting commit.
        # Still worth a provenance link: identical content is exactly the case where git has
        # no other way to learn that this branch already carries the sender's work.
        record_provenance(commit, cwd)
        return GitResult(True, head_commit(cwd), "", 0)

    committed = run_git(["commit", "-m", message], cwd)
    if not committed.ok:
        return committed
    record_provenance(commit, cwd)
    return GitResult(True, head_commit(cwd), "", 0)


def _squash_attempt(commit: str, cwd: str | Path) -> GitResult:
    args = ["merge", "--squash", commit]
    result = run_git(args, cwd)
    if not result.ok and _clear_generated_blockers(result.output, cwd):
        return run_git(args, cwd)
    return result


def already_contains(target: str, cwd: str | Path) -> bool:
    """
    True when `target` is already reachable from HEAD, so merging it would do nothing.

    Load-bearing, not an optimisation. `merge_commit` relies on `--no-ff` producing a merge
    commit for `squash_anchor` to find; but `git merge --no-ff <ancestor>` reports "Already up
    to date" and creates *no* commit. Calling it on a target we already have would therefore
    leave the cycle with no anchor, and `squash_anchor` falls back to the repository's ROOT --
    which the next `reset --soft` collapses the entire project history into.

    So the caller must ask this first and skip the merge when it answers True. Skipping is
    also simply correct: there is nothing to bring in.

    An unresolvable ref answers False, so an unknown branch reaches `merge_commit` and fails
    there with git's own message rather than being silently treated as already-merged.
    """
    return run_git(["merge-base", "--is-ancestor", target, "HEAD"], cwd).ok


def record_provenance(commit: str, cwd: str | Path) -> GitResult:
    """
    Link `commit` into this branch's ancestry without changing a single file.

    `merge --squash` above copies content and drops parentage, and that missing parentage is
    what makes the *next* lap conflict. Observed live on a three-cycle run: the coder's own
    `books.py` reached the refactorer and the architect, then came back to the coder through
    the squash as content with no history. Git computed the merge base as the specifier's
    feature-file commit -- from *before* any implementation existed -- so it saw the same file
    created independently on both sides and reported a content conflict. The coder was being
    asked to reconcile its own work with a refactored copy of its own work.

    `-s ours` keeps this branch's tree exactly as the squash left it and records the sender as
    a second parent, so later merge bases tell the truth. Deliberately not `--no-ff`: when the
    sender is already an ancestor there is nothing to record and git's "Already up to date"
    is the correct no-op.

    The trade, stated plainly because avoiding it is why the squash exists: `@current` gains
    one merge commit per lap. It does *not* gain the sender's commits on its first-parent
    line -- `git log --first-parent` still reads as one flat commit per handoff, which is the
    shape the squash was protecting. What changes is that those commits become *reachable*,
    which is the whole point.

    Safe on a branch someone is editing, which `human-in-the-loop`'s always might be: a normal
    merge refuses when it would overwrite uncommitted changes, but `-s ours` never would --
    the result tree *is* HEAD's tree -- so git runs it and leaves their edit untouched in the
    working tree. Verified, not assumed.

    Best effort even so. The content is already committed by the time this runs, so anything
    that goes wrong here costs a truthful merge base, not the handoff -- a warning, not an
    error, and never an exception into the inbox.
    """
    message = f"Record provenance of {commit[:8]} (history link only, no content change)"
    result = run_git(["merge", "-s", "ours", "-m", message, commit], cwd)
    if not result.ok:
        first_line = (result.output.strip().splitlines() or [""])[0]
        log.warning(
            "could not record provenance for %s: %s -- later merges against this branch will "
            "compute an older merge base and may conflict on files both sides touched",
            commit[:8],
            first_line,
        )
    return result


def is_generated_path(path: str, patterns: tuple[str, ...] = GENERATED_WORKTREE_PATHS) -> bool:
    """True when `path` is a launcher artefact rather than project content."""
    candidate = path.replace("\\", "/").removeprefix("./")
    for pattern in patterns:
        if pattern.endswith("/"):
            if candidate == pattern.rstrip("/") or candidate.startswith(pattern):
                return True
        elif fnmatch(candidate, pattern):
            return True
    return False


def blocking_untracked(output: str) -> list[str]:
    """
    Paths git named as untracked files standing in the way of a merge.

    git prints them indented under the error line and ends the list with an unindented
    "Please move or remove them..."; anything else means this is a different failure.
    """
    if _UNTRACKED_BLOCKER not in output:
        return []

    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if _UNTRACKED_BLOCKER in line) + 1
    return _indented_paths(lines[start:])


def _indented_paths(lines: list[str]) -> list[str]:
    paths = []
    for line in lines:
        if not line[:1].isspace() or not line.strip():
            break
        paths.append(line.strip())
    return paths


def _clear_generated_blockers(output: str, cwd: str | Path) -> list[str]:
    """
    Delete merge-blocking untracked files, but only when every one is Kiln-generated.

    All-or-nothing on purpose: one unrecognised path means the worker (or the user) left
    something real there, and silently deleting it would be far worse than a failed merge.
    """
    blockers = _generated_blockers(output)
    if not blockers:
        return []
    removed = [
        relative for relative in blockers if _remove_generated_file(Path(cwd) / relative, relative)
    ]

    if removed:
        log.warning("cleared launcher-generated file(s) blocking the merge: %s", ", ".join(removed))
    return removed


def _generated_blockers(output: str) -> list[str]:
    blockers = blocking_untracked(output)
    return blockers if blockers and all(is_generated_path(path) for path in blockers) else []


def _remove_generated_file(target: Path, relative: str) -> bool:
    try:
        if not target.is_file():
            return False
        target.unlink()
        return True
    except OSError as exc:
        log.warning("could not remove %s: %s", relative, exc)
        return False


def is_ignored(pattern: str, cwd: str | Path) -> bool:
    """True when git already ignores `pattern`."""
    return run_git(["check-ignore", "-q", pattern], cwd).returncode == 0


def ensure_ignored(pattern: str, cwd: str | Path) -> None:
    """
    Guarantee a path is ignored, via the repo-local exclude file.

    The scheduler writes `tmp/handoff-in.md` every cycle for debuggability. If the project
    does not ignore `tmp/`, that file is swept into the squash commit by `git add -A`, and
    then the NEXT cycle's merge aborts with "untracked working tree files would be
    overwritten" — the scheduler deadlocking itself on its own debug artefact.

    See `ensure_generated_ignored` for the full set this protects.

    `.git/info/exclude` is used rather than `.gitignore` because it is local-only and never
    commits a change to the user's project. `--git-path` resolves correctly inside linked
    worktrees, where `.git` is a file rather than a directory.
    """
    if is_ignored(pattern, cwd):
        return

    located = run_git(["rev-parse", "--git-path", "info/exclude"], cwd)
    if not located.ok or not located.stdout:
        log.warning("could not locate info/exclude; %s may be committed accidentally", pattern)
        return

    exclude_path = _git_path(cwd, located.stdout)
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        _append_ignore_pattern(exclude_path, existing, pattern)
    except OSError as exc:
        log.warning("could not update %s: %s", exclude_path, exc)


def _append_ignore_pattern(exclude_path: Path, existing: str, pattern: str) -> None:
    if pattern in existing.splitlines():
        return
    separator = "" if existing.endswith("\n") or not existing else "\n"
    exclude_path.write_text(f"{existing}{separator}{pattern}\n", encoding="utf-8")
    log.info("added %r to %s", pattern, exclude_path)


def ensure_generated_ignored(cwd: str | Path) -> None:
    """
    Force-ignore every launcher artefact before the first `git add -A` can reach it.

    Cheap when the project's own `.gitignore` already covers them — `is_ignored` short-
    circuits — but it is the only thing protecting a project scaffolded by an older Kiln,
    whose committed `.gitignore` predates entries added since.
    """
    for pattern in GENERATED_WORKTREE_PATHS:
        ensure_ignored(pattern, cwd)


#: Files every role appends to independently, in its own worktree, which therefore conflict
#: on every merge unless git is told they are append-only.
#:
#: `logbook.md` is the whole list today: `/kiln-receive`, `/kiln-handoff` and `/kiln-ping` all
#: instruct the agent to append a line and commit it. Two branches adding different lines to
#: the end of one tracked file is the classic changelog conflict, and it fires every cycle
#: regardless of what the swarm is actually building.
#:
#: It degrades further than a normal textual conflict. The squash mechanics -- `reset --soft`
#: per role, `merge --squash` onto the human's branch -- leave commits with no link back to
#: where their content came from, so the merge base often has no `logbook.md` at all. Git then
#: sees the file as independently created on both sides and reports `add/add`, which it will
#: not attempt to merge. Observed live twice; the first time it was attributed to a stale
#: worktree branch (see `workspace.warn_if_worktree_conflicts`), which is a real but different
#: problem.
UNION_MERGE_PATHS = ("logbook.md",)


def ensure_union_merge(cwd: str | Path, paths: tuple[str, ...] = UNION_MERGE_PATHS) -> None:
    """
    Declare append-only files union-merged, via the repo-local attributes file.

    `union` keeps both sides' lines instead of conflicting -- exactly right for a log nobody
    reads for structure, and it resolves the `add/add` case as well as ordinary divergence.

    `.git/info/attributes` rather than a committed `.gitattributes`, for the same reason
    `ensure_ignored` uses `info/exclude`: it changes nothing in the user's project. It also
    takes effect *immediately* in every worktree, because `--git-path` resolves to the shared
    common directory even from inside a linked worktree -- a committed `.gitattributes` would
    only apply once it had propagated onto each role's branch, which is several merges too
    late for the merges it exists to fix.
    """
    located = run_git(["rev-parse", "--git-path", "info/attributes"], cwd)
    if not located.ok or not located.stdout:
        log.warning("could not locate info/attributes; %s may conflict on merge", paths)
        return

    attributes_path = _git_path(cwd, located.stdout)
    try:
        existing = attributes_path.read_text(encoding="utf-8") if attributes_path.exists() else ""
        missing = _missing_union_rules(existing, paths)
        if not missing:
            return
        _write_union_rules(attributes_path, existing, missing)
        log.info("declared %s union-merged in %s", ", ".join(paths), attributes_path)
    except OSError as exc:
        log.warning("could not update %s: %s", attributes_path, exc)


def _write_union_rules(attributes_path: Path, existing: str, missing: list[str]) -> None:
    attributes_path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if existing.endswith("\n") or not existing else "\n"
    attributes_path.write_text(
        f"{existing}{separator}" + "\n".join(missing) + "\n", encoding="utf-8"
    )


def _git_path(cwd: str | Path, path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else Path(cwd) / candidate


def _missing_union_rules(existing: str, paths: tuple[str, ...]) -> list[str]:
    lines = existing.splitlines()
    return [f"{path} merge=union" for path in paths if f"{path} merge=union" not in lines]


def squash_anchor(cwd: str | Path) -> str:
    """
    The commit to squash back onto: the most recent merge, else the repo's root commit.

    Mirrors kiln-handoff/SKILL.md step 2, which picks `git log --merges -1` and falls back
    to the root commit when a worktree has not merged anything yet.
    """
    merges = run_git(["log", "--merges", "-1", "--format=%H"], cwd)
    if merges.ok and merges.stdout:
        return merges.stdout

    root = run_git(["rev-list", "--max-parents=0", "HEAD"], cwd)
    return root.stdout.splitlines()[0] if root.ok and root.stdout else ""


def has_commits_since(anchor: str, cwd: str | Path) -> bool:
    """True when work exists to squash. Ping cycles legitimately produce nothing."""
    result = run_git(["rev-list", "--count", f"{anchor}..HEAD"], cwd)
    return result.ok and result.stdout.isdigit() and int(result.stdout) > 0


def has_pending_changes(cwd: str | Path) -> bool:
    """True when the worktree has uncommitted changes (staged or not)."""
    result = run_git(["status", "--porcelain"], cwd)
    return result.ok and bool(result.stdout)


def squash_since(anchor: str, message: str, cwd: str | Path) -> GitResult:
    """
    Collapse everything since `anchor` into one commit, returning the new hash in stdout.

    Uncommitted work is staged first so a worker that edited files without committing does
    not silently lose its changes — the legacy prose assumed the worker committed, and a
    one-shot worker frequently does not.
    """
    staged = _stage_pending(cwd)
    if staged is not None and not staged.ok:
        return staged

    if _nothing_to_squash(anchor, cwd):
        # Nothing to squash. Report the existing HEAD so the caller still has a commit to
        # reference in its handoff rather than treating this as a failure.
        current = head_commit(cwd)
        log.info("nothing to squash since %s; reusing HEAD %s", anchor, current)
        return GitResult(True, current, "", 0)

    return _squash_commits(anchor, message, cwd)


def _nothing_to_squash(anchor: str, cwd: str | Path) -> bool:
    return not has_commits_since(anchor, cwd) and not has_pending_changes(cwd)


def _squash_commits(anchor: str, message: str, cwd: str | Path) -> GitResult:
    reset = run_git(["reset", "--soft", anchor], cwd)
    if not reset.ok:
        return reset

    committed = run_git(["commit", "-m", message], cwd)
    if not committed.ok:
        return committed

    return GitResult(True, head_commit(cwd), "", 0)


def _stage_pending(cwd: str | Path) -> GitResult | None:
    return run_git(["add", "-A"], cwd) if has_pending_changes(cwd) else None


def commit_all(message: str, cwd: str | Path) -> GitResult:
    """Stage and commit everything, returning the new hash in stdout. No-op when clean."""
    if not has_pending_changes(cwd):
        return GitResult(True, head_commit(cwd), "", 0)

    staged = run_git(["add", "-A"], cwd)
    if not staged.ok:
        return staged

    committed = run_git(["commit", "-m", message], cwd)
    if not committed.ok:
        return committed
    return GitResult(True, head_commit(cwd), "", 0)
