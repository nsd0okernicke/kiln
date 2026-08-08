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
#: Mirrors launcher.workspace.REQUIRED_GITIGNORE_ENTRIES, duplicated on purpose so the
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


def merge_commit(commit: str, cwd: str | Path) -> GitResult:
    """
    Merge the sender's commit. The resulting merge commit becomes the squash anchor.

    A failure here must stop the cycle before any delegation — per /kiln-receive step 4,
    "if the merge fails, stop and report the error before proceeding". Working on top of a
    conflicted tree would produce a handoff nobody can use.

    `--no-ff` is essential, not cosmetic. A fast-forward merge creates no merge commit, so
    `squash_anchor` would find none and fall back to the repository's ROOT commit — and the
    next `git reset --soft` would then collapse the entire project history into a single
    commit. Forcing a merge commit guarantees every cycle has a well-defined anchor.
    """
    result = run_git(["merge", "--no-ff", "--no-edit", commit], cwd)
    if result.ok:
        return result

    # A merge blocked *only* by Kiln's own scaffolding is recoverable: the incoming commit
    # carries the same generated content, and the launcher rewrites these files on the next
    # launch anyway. Retry once after clearing them rather than escalating a deadlock that
    # a human could only fix with the same `rm`.
    if _clear_generated_blockers(result.output, cwd):
        result = run_git(["merge", "--no-ff", "--no-edit", commit], cwd)
        if result.ok:
            return result

    log.error("merge of %s failed: %s", commit, result.output)
    # Leave no half-merged tree behind for the next cycle to trip over.
    run_git(["merge", "--abort"], cwd)
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

    paths: list[str] = []
    collecting = False
    for line in output.splitlines():
        if _UNTRACKED_BLOCKER in line:
            collecting = True
            continue
        if not collecting:
            continue
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
    blockers = blocking_untracked(output)
    if not blockers or not all(is_generated_path(path) for path in blockers):
        return []

    removed: list[str] = []
    for relative in blockers:
        target = Path(cwd) / relative
        try:
            if target.is_file():
                target.unlink()
                removed.append(relative)
        except OSError as exc:
            log.warning("could not remove %s: %s", relative, exc)

    if removed:
        log.warning(
            "cleared launcher-generated file(s) blocking the merge: %s", ", ".join(removed)
        )
    return removed


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

    exclude_path = Path(cwd) / located.stdout if not Path(located.stdout).is_absolute() else Path(
        located.stdout
    )
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if pattern not in existing.splitlines():
            separator = "" if existing.endswith("\n") or not existing else "\n"
            exclude_path.write_text(
                f"{existing}{separator}{pattern}\n", encoding="utf-8"
            )
            log.info("added %r to %s", pattern, exclude_path)
    except OSError as exc:
        log.warning("could not update %s: %s", exclude_path, exc)


def ensure_generated_ignored(cwd: str | Path) -> None:
    """
    Force-ignore every launcher artefact before the first `git add -A` can reach it.

    Cheap when the project's own `.gitignore` already covers them — `is_ignored` short-
    circuits — but it is the only thing protecting a project scaffolded by an older Kiln,
    whose committed `.gitignore` predates entries added since.
    """
    for pattern in GENERATED_WORKTREE_PATHS:
        ensure_ignored(pattern, cwd)


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
    if has_pending_changes(cwd):
        staged = run_git(["add", "-A"], cwd)
        if not staged.ok:
            return staged

    if not has_commits_since(anchor, cwd) and not has_pending_changes(cwd):
        # Nothing to squash. Report the existing HEAD so the caller still has a commit to
        # reference in its handoff rather than treating this as a failure.
        current = head_commit(cwd)
        log.info("nothing to squash since %s; reusing HEAD %s", anchor, current)
        return GitResult(True, current, "", 0)

    reset = run_git(["reset", "--soft", anchor], cwd)
    if not reset.ok:
        return reset

    committed = run_git(["commit", "-m", message], cwd)
    if not committed.ok:
        return committed

    return GitResult(True, head_commit(cwd), "", 0)


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
