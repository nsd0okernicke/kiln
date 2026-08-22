"""
Workspace preparation: gitignore, repo init, hooks, worktrees, skills.

Ports Ensure-InitialGitignore, Initialize-GitRepo, Install-GitHooks, Prepare-Workspace,
Prepare-Worktrees, Prepare-Skills, Prepare-AgentConfigs and Prepare-CodexConfigs.

Several of the odd-looking details here are load-bearing and carry their reasons inline —
most were learned the hard way (a tracked `.kiln` symlink breaking later merges, a stray
`kiln/.gitignore` excluding the whole constitution from every worktree).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from scheduler import git_ops

from .config import Profile, RoleConfig
from .generate import (
    build_codex_config_toml,
    build_copilot_mcp_config,
    channel_is_available,
    write_mcp_config,
)
from .paths import KilnPaths

log = logging.getLogger(__name__)

#: Patterns every Kiln project needs ignored.
#:
#: `.kiln` deliberately has NO trailing slash: it is a symlink in each worktree, and a
#: trailing-slash pattern only matches real directories — with the slash the symlink stays
#: untracked-but-not-ignored and can be swept into a commit, breaking later merges.
#:
#: CLAUDE.md / AGENTS.md / .mcp.json / tmp/ are regenerated per role with different content,
#: so tracking them makes every role's copy differ and turns each merge into an add/add
#: conflict.
#:
#: `.claude/settings.json` is copied into every worktree from the same template. Left
#: trackable, the scheduler's `git add -A` commits one role's copy and every other role's
#: next merge aborts on it. Observed live; see git_ops.GENERATED_WORKTREE_PATHS.
REQUIRED_GITIGNORE_ENTRIES = (
    ".kiln",
    ".worktrees/",
    ".github/",
    ".claude/settings.json",
    ".claude/skills",
    ".agents/skills",
    ".claude/agents/*-worker.md",
    ".codex/agents/*-worker.toml",
    ".grok/agents/*-worker.md",
    "CLAUDE.md",
    "AGENTS.md",
    ".mcp.json",
    "tmp/",
)

BASE_GITIGNORE = (
    ".DS_Store\n.env\n.env.local\n*.pyc\n__pycache__/\n*.egg-info/\ndist/\nbuild/\n"
    ".pytest_cache/\n.coverage\nhtmlcov/\n.mypy_cache/\n.ruff_cache/\n.venv/\nvenv/\n"
    ".idea/\n.vscode/\n*.swp\n*.swo\n*~\n"
)

PRE_PUSH_HOOK = """\
#!/bin/sh
BRANCH_LIST="$(git rev-parse --git-dir)/kiln-sub-branches"
while read local_ref local_sha remote_ref remote_sha; do
  branch="${local_ref#refs/heads/}"
  if [ -f "$BRANCH_LIST" ] && grep -qxF "$branch" "$BRANCH_LIST"; then
    echo "error: '$branch' is a Kiln sub-branch and cannot be pushed."
    exit 1
  fi
done
exit 0
"""


class WorkspaceError(Exception):
    """Setup failed in a way that must stop the launch."""


def run_git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def write_directory_gitignore(directory: Path) -> None:
    """Drop a self-ignoring `*` gitignore into an ephemeral directory."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")


#: Committed counterpart to `git_ops.ensure_union_merge`'s local-only repair.
#:
#: Both exist on purpose. The `info/attributes` write is what actually unblocks a *running*
#: swarm -- it is shared across worktrees and effective immediately. This file is what carries
#: the rule to a fresh clone, another machine, or a human merging these branches by hand,
#: none of which see a local-only attributes file. Same split as `.gitignore` (committed) and
#: `info/exclude` (local repair).
GITATTRIBUTES = (
    "# Every role appends to logbook.md in its own worktree, so two branches always add\n"
    "# different lines to the end of one file. `union` keeps both instead of conflicting.\n"
    "logbook.md merge=union\n"
)


def ensure_gitattributes(paths: KilnPaths) -> Path:
    """Create or top up the project `.gitattributes` with the entries Kiln requires."""
    path = paths.project_root / ".gitattributes"
    required = [
        line for line in GITATTRIBUTES.splitlines() if line and not line.startswith("#")
    ]
    if not path.exists():
        path.write_text(GITATTRIBUTES, encoding="utf-8")
        return path

    existing = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    missing = [entry for entry in required if entry not in existing]
    if missing:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + "\n".join(missing) + "\n")
    return path


def ensure_gitignore(paths: KilnPaths) -> Path:
    """Create or top up the project .gitignore with the entries Kiln requires."""
    path = paths.project_root / ".gitignore"
    if not path.exists():
        path.write_text(
            BASE_GITIGNORE + "\n".join(REQUIRED_GITIGNORE_ENTRIES) + "\n", encoding="utf-8"
        )
        return path

    existing = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    missing = [entry for entry in REQUIRED_GITIGNORE_ENTRIES if entry not in existing]
    if missing:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + "\n".join(missing) + "\n")
    return path


def install_git_hooks(paths: KilnPaths) -> Path | None:
    """
    Install the pre-push hook that blocks pushing Kiln sub-branches.

    Written as BOM-less UTF-8 with LF endings: a BOM breaks git's shebang detection and the
    push fails with "cannot spawn .git/hooks/pre-push: No such file or directory".
    """
    hook = paths.project_root / ".git" / "hooks" / "pre-push"
    if hook.exists():
        return hook
    if not hook.parent.parent.exists():
        return None

    hook.parent.mkdir(parents=True, exist_ok=True)
    with hook.open("wb") as handle:
        handle.write(PRE_PUSH_HOOK.replace("\r\n", "\n").encode("utf-8"))
    if os.name != "nt":
        make_executable(hook)
    return hook


def make_executable(path: Path) -> None:
    """
    `chmod +x`, tolerating filesystems that refuse the call.

    git silently skips a hook that is not executable, so this genuinely matters on a normal
    filesystem. But a WSL DrvFs mount of a Windows drive rejects `chmod` outright with EPERM
    while already reporting every file as `rwxrwxrwx` — so the call was pure ceremony there
    and yet took the entire launch down with a raw traceback (confirmed on Ubuntu 24.04,
    launching against a `/mnt/c/...` project).

    Only warn when the file really is not executable afterwards: that is the case that
    actually costs the user a working hook, and it is worth a message rather than silence.
    """
    try:
        path.chmod(0o755)
    except OSError as exc:
        if not os.access(path, os.X_OK):
            log.warning(
                "could not make %s executable (%s); git will silently skip this hook",
                path, exc,
            )


def initialize_repo(paths: KilnPaths) -> None:
    """
    Make sure the project is a git repo with `.gitignore` committed.

    The `.gitignore` must be tracked *before* any worktree is created: `git worktree add`
    only checks out tracked content, so otherwise each worktree starts without it and the
    next broad `git add` there can track the `.kiln` symlink — which later breaks merges
    that try to check that symlink out over a locked messages.db.
    """
    is_new = not (paths.project_root / ".git").exists()
    if is_new:
        run_git(["init"], paths.project_root, check=True)
        run_git(["branch", "-M", "main"], paths.project_root)

    ensure_gitignore(paths)
    ensure_gitattributes(paths)
    # Every launch, not just a new project: this is what repairs a project scaffolded before
    # the rule existed, and the file it writes is local-only and shared across worktrees, so
    # one call here covers every role for the whole run -- without waiting for the committed
    # `.gitattributes` above to propagate onto each role's branch.
    git_ops.ensure_union_merge(paths.project_root)

    if is_new:
        run_git(["add", "."], paths.project_root)
        run_git(["commit", "-m", "Initial kiln repository"], paths.project_root)
        if not run_git(["rev-parse", "HEAD"], paths.project_root).returncode == 0:
            raise WorkspaceError(
                "initial git commit failed — check `git config user.email` and `user.name`"
            )
        return

    tracked = run_git(["ls-files", "--error-unmatch", ".gitignore"], paths.project_root)
    if tracked.returncode != 0:
        run_git(["add", ".gitignore"], paths.project_root)
        run_git(
            ["commit", "-m", "kiln: ensure .gitignore is tracked before creating worktrees"],
            paths.project_root,
        )


def warn_if_kiln_untracked(paths: KilnPaths) -> bool:
    """
    Warn when `kiln/` is not committed, before worktrees are created from HEAD.

    `git worktree add` only checks out *tracked* content, so an untracked `kiln/` produces
    worktrees with no constitution, roles or skills at all. This is the failure mode that
    leaves a project looking scaffolded while every worktree is missing its rules, and it
    is silent unless something says so. Returns True when a warning was issued.
    """
    if not paths.kiln_project_dir.is_dir():
        return False

    tracked = run_git(["ls-files", "kiln/"], paths.project_root)
    if tracked.returncode == 0 and tracked.stdout.strip():
        return False

    log.warning(
        "kiln/ is not committed. `git worktree add` only checks out tracked files, so every "
        "worktree will be created WITHOUT the constitution, roles and skills."
    )
    log.warning("   Fix before launching:  git add kiln/ && git commit -m 'kiln scaffolding'")
    return True


def current_branch(paths: KilnPaths, fallback: str = "kiln") -> str:
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], paths.project_root)
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else fallback


def prepare_state_dirs(paths: KilnPaths) -> None:
    """Create `.kiln/` and `.worktrees/`, and repair a known bad generated file."""
    for directory in (paths.state_dir, paths.worktrees_dir):
        write_directory_gitignore(directory)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.status_dir.mkdir(parents=True, exist_ok=True)

    # `kiln/` is version-controlled — it holds the constitution and roles every worktree
    # needs to check out. An earlier version blanket-ignored it, which silently excluded
    # kiln/project/ from every commit and left new worktrees with no kiln/ at all. Self-heal
    # projects still carrying that file (fingerprint: a lone "*").
    stray = paths.kiln_dir / ".gitignore"
    if stray.is_file() and stray.read_text(encoding="utf-8").strip() == "*":
        stray.unlink()
        log.warning("removed stray kiln/.gitignore that was excluding kiln/project/")


def copy_template_file(source: Path, destination: Path) -> None:
    """
    Copy a framework file into a project — contents only, no metadata.

    `shutil.copy2` was used at every one of these call sites, and it replays the source's
    mode and mtime onto the copy via `chmod`/`utime`. Both are rejected with `EPERM` on a WSL
    DrvFs mount of a Windows drive whenever the mount maps files to a different uid than the
    calling user, so `kiln init` into a `/mnt/c/...` directory died on a raw PermissionError
    traceback having already created the directory tree — a half-scaffolded project and a
    stack trace (confirmed on Ubuntu 24.04). Network shares and container volume mounts
    reject the same calls.

    Nothing Kiln copies needs either attribute: these are `.md`/`.json` templates, read by
    agents and by Kiln itself and never executed by path. A brand-new project's files
    carrying the framework checkout's modification times would be actively misleading.
    """
    shutil.copyfile(source, destination)


def copy_template_tree(source: Path, destination: Path) -> None:
    """
    Recursively copy a framework directory into a project — contents only.

    `shutil.copytree` cannot be rescued by passing `copy_function=copyfile`: it always
    finishes by calling `copystat` on each directory it created, collects the resulting
    error and raises `shutil.Error`. That is the very call `copy_template_file` exists to
    avoid, so the walk is done here instead.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_template_file(item, target)


def copy_framework_tools(paths: KilnPaths) -> None:
    """Refresh `.kiln/tools/` so agents can invoke set-status.py."""
    if not paths.framework_tools_dir.is_dir():
        return
    paths.state_tools_dir.mkdir(parents=True, exist_ok=True)
    for item in paths.framework_tools_dir.iterdir():
        if item.is_file():
            copy_template_file(item, paths.state_tools_dir / item.name)


def _link_or_copy(link: Path, target: Path, label: str) -> None:
    """
    Point `link` at `target`, falling back to a copy when symlinks are unavailable.

    Windows needs Developer Mode or elevation for symlinks, so the fallback is a real
    scenario rather than defensive padding.
    """
    if link.is_symlink() or link.exists():
        if link.is_symlink() or link.is_file():
            link.unlink()
        else:
            shutil.rmtree(link, ignore_errors=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        # Windows needs Developer Mode or elevation for symlinks; the copy keeps the swarm
        # working, at the cost of per-worktree duplicates instead of shared state.
        log.warning(
            "[%s] could not symlink %s -> %s (%s); copying instead", label, link, target, exc
        )
        link.mkdir(parents=True, exist_ok=True)
        if (target / "tools").is_dir():
            copy_template_tree(target / "tools", link / "tools")
        (link / "logs").mkdir(parents=True, exist_ok=True)


def warn_if_worktree_conflicts(paths: KilnPaths, role_branch: str, branch: str) -> bool:
    """
    Warn when an existing worktree's branch would conflict with the current branch tip.

    Uses `git merge-tree --write-tree` as a non-destructive dry run -- it touches no working
    tree or branch, so it's safe to run before any pane opens. Exit code 1 means Git found
    real content conflicts, not just "these branches have diverged" (which is normal: a role's
    branch is expected to be behind until it processes its next handoff). A conflict here means
    the two histories independently created overlapping content, which is the signature of a
    stale worktree from an abandoned run rather than one that's simply behind on this run's
    handoffs. Returns True when a warning was issued.
    """
    result = run_git(["merge-tree", "--write-tree", role_branch, branch], paths.project_root)
    if result.returncode != 1:
        return False

    conflicts = [line.strip() for line in result.stdout.splitlines() if line.startswith("CONFLICT")]
    log.warning(
        "worktree branch %r would conflict with the current %r branch tip -- this looks like "
        "a worktree left over from an earlier, unrelated run rather than one that's just behind "
        "on this run's handoffs:",
        role_branch, branch,
    )
    for conflict in conflicts:
        log.warning("   %s", conflict)
    log.warning(
        "   If so, stop the swarm and remove the stale worktree directory before relaunching "
        "so it gets recreated fresh from the current branch tip."
    )
    return True


def prepare_worktrees(profile: Profile, paths: KilnPaths, branch: str) -> list[Path]:
    """Create one git worktree per non-@current role and wire its per-role config."""
    run_git(["worktree", "prune"], paths.project_root)

    sub_branch_list = paths.project_root / ".git" / "kiln-sub-branches"
    sub_branches: list[str] = []
    created: list[Path] = []

    for role in profile.roles:
        if role.uses_current_dir:
            continue

        worktree = paths.worktree_path(role.worktree)
        role_branch = role.branch_name(branch)

        if not worktree.exists():
            result = run_git(
                ["worktree", "add", "--force", "-B", role_branch, str(worktree), "HEAD"],
                paths.project_root,
            )
            if result.returncode != 0:
                raise WorkspaceError(
                    f"could not create worktree for role {role.role!r}: {result.stderr.strip()}"
                )
        else:
            # `.worktrees/` is gitignored (see REQUIRED_GITIGNORE_ENTRIES) and so survives a
            # `branch` reset/reinit untouched, while the branch above is only (re)created when
            # the worktree directory is absent -- an existing worktree left over from an
            # earlier, unrelated run silently carries its old branch straight into this one.
            # A dry-run merge against the current branch tip surfaces that here, before any
            # pane opens, instead of failing later as a real conflicted `git merge` inside
            # kiln-receive (observed live: an add/add conflict on logbook.md).
            warn_if_worktree_conflicts(paths, role_branch, branch)
        created.append(worktree)

        _link_or_copy(worktree / ".kiln", paths.state_dir, role.role)
        _copy_settings(paths, worktree)
        _copy_worker_definitions(paths, worktree)
        # See generate.channel_is_available for why a role may not get the blocking channel.
        write_mcp_config(
            worktree, paths, role.role, branch,
            include_channel=channel_is_available(role),
        )
        (worktree / "tmp").mkdir(parents=True, exist_ok=True)
        sub_branches.append(role_branch)

    if sub_branch_list.parent.exists():
        sub_branch_list.write_text("\n".join(sub_branches) + "\n", encoding="utf-8")
    return created


def _copy_settings(paths: KilnPaths, worktree: Path) -> None:
    template = paths.claude_settings_template
    if not template.is_file():
        log.warning("Claude settings template not found at %s", template)
        return
    target = worktree / ".claude"
    target.mkdir(parents=True, exist_ok=True)
    copy_template_file(template, target / "settings.json")


def _copy_worker_definitions(paths: KilnPaths, worktree: Path) -> None:
    """Mirror generated worker files into the worktree so each CLI can discover them."""
    for relative, pattern in (
        (Path(".claude") / "agents", "*-worker.md"),
        (Path(".github") / "agents", "*-worker.agent.md"),
        (Path(".codex") / "agents", "*-worker.toml"),
        (Path(".grok") / "agents", "*-worker.md"),
    ):
        source = paths.project_root / relative
        destination = worktree / relative
        destination.mkdir(parents=True, exist_ok=True)
        if not source.is_dir():
            continue
        for item in source.glob(pattern):
            copy_template_file(item, destination / item.name)


#: Every project-level convention a supported CLI might scan for skills, relative to a
#: worktree root. Confirmed live via `copilot skill --help`: Copilot alone checks all three
#: ("Project .github/skills/, .agents/skills/, or .claude/skills/"), not just the one usually
#: thought of as "its own" -- so a directory left over from an earlier profile/agent change
#: (e.g. a role that used to be `agent: claude` leaving `.claude/skills` behind after switching
#: to `agent: copilot`) is just as visible to Copilot as `.github/skills`. Every convention is
#: therefore kept in sync in every worktree, not just whichever one nominally belongs to that
#: role's current agent.
#:
#: Grok needs no fourth entry. Verified with `grok inspect` against 1.0.5: it discovers
#: project skills from `.claude/skills` and `.agents/skills` (and a `.grok/skills` of its own,
#: which Kiln has no reason to populate as well) but NOT `.github/skills` -- so the two it
#: shares with this list already reach it, including the wrapper-only filtering below.
SKILL_DIR_CONVENTIONS = (
    (".claude", "skills"),
    (".github", "skills"),
    (".agents", "skills"),
)

#: Skills that only make sense inside an interactive wrapper session, which discovers a
#: handoff/message by reading these skills itself. A scheduler-mode worker never needs them --
#: the scheduler handles receive/merge/handoff mechanically in Python -- and a one-shot worker
#: that sees them anyway has been observed trying to follow their message-queue protocol
#: (checking tmp/handoff-in.md, hunting for the kiln-channel MCP server) against MCP access
#: the scheduler adapter deliberately disables, turning an expected denial into a confused,
#: credit-burning session that never produces a result.
WRAPPER_ONLY_SKILLS = frozenset({"kiln-receive", "kiln-handoff", "kiln-ping"})


def prepare_skills(profile: Profile, paths: KilnPaths) -> int:
    """Link every project skill into each role's worktree, across every skills-directory
    convention a backend's CLI might scan."""
    if not paths.skills_dir.is_dir():
        return 0
    skills = [item for item in paths.skills_dir.iterdir() if item.is_dir()]
    if not skills:
        return 0

    targets: list[tuple[Path, bool]] = [
        (
            paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree),
            role.uses_scheduler,
        )
        for role in profile.roles
        if role.agent in ("claude", "copilot", "codex", "grok")
    ]
    if targets:
        targets.append((paths.project_root, False))

    linked = 0
    for root, uses_scheduler in targets:
        for parent, name in SKILL_DIR_CONVENTIONS:
            skills_dir = root / parent / name
            # Always recreate so removed skills -- and stale ones from an earlier agent --
            # do not linger.
            if skills_dir.exists():
                shutil.rmtree(skills_dir, ignore_errors=True)
            skills_dir.mkdir(parents=True, exist_ok=True)
            for skill in skills:
                if uses_scheduler and skill.name in WRAPPER_ONLY_SKILLS:
                    continue
                try:
                    (skills_dir / skill.name).symlink_to(skill, target_is_directory=True)
                    linked += 1
                except (OSError, NotImplementedError):
                    copy_template_tree(skill, skills_dir / skill.name)
                    linked += 1
    return linked


def _trust_copilot_worktrees(profile: Profile, paths: KilnPaths) -> None:
    """
    Add every copilot-agent role's worktree to `~/.copilot/config.json`'s `trustedFolders`.

    Copilot gates every write-capable tool behind a workspace-trust check that is separate
    from `--allow-all` and consulted per literal directory path -- observed live: a git
    worktree is a different path from the project root even though it shares history, so
    trusting the root does not trust the worktree. Every write tool (including the `kiln-db`
    MCP tool) returned "Permission denied and could not request permission from user" for 8+
    minutes before the worker gave up having made zero changes, consuming real AI credits the
    whole time reading code and retrying writes that could never succeed non-interactively.

    Additive and defensive: preserves whatever folders the user has already trusted
    interactively, and backs off entirely (logging a warning, never raising) if the file is
    missing or doesn't parse -- trust is a security-relevant setting the user owns; this only
    appends what Kiln's own worktrees need, the same way `prepare_agent_configs()` already
    manages `mcp-config.json` in the same directory.
    """
    worktrees = {
        str(paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree))
        for role in profile.roles
        if role.agent == "copilot"
    }
    if not worktrees:
        return

    config_path = Path.home() / ".copilot" / "config.json"
    if not config_path.is_file():
        log.warning(
            "no %s found; copilot worker writes in an untrusted worktree may be silently "
            "denied -- run `copilot` interactively once first to create it",
            config_path,
        )
        return

    try:
        raw = config_path.read_text(encoding="utf-8")
        brace = raw.index("{")
        header, body = raw[:brace], json.loads(raw[brace:])
    except (OSError, ValueError) as exc:
        log.warning("could not read %s to trust worktrees: %s", config_path, exc)
        return

    trusted = set(body.get("trustedFolders") or [])
    missing = worktrees - trusted
    if not missing:
        return

    body["trustedFolders"] = sorted(trusted | missing)
    try:
        config_path.write_text(header + json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s to trust worktrees: %s", config_path, exc)
        return
    log.info("trusted for copilot: %s", ", ".join(sorted(missing)))


def prepare_agent_configs(profile: Profile, paths: KilnPaths) -> None:
    """
    Write the per-backend configs the worktree's own `.mcp.json` cannot cover.

    Only two backends need this. Copilot reads one global `~/.copilot/mcp-config.json` plus a
    trusted-folder list; Codex reads a relocated `CODEX_HOME/config.toml` per role. Claude and
    Grok both read the worktree `.mcp.json` that `prepare_worktrees` already writes — Grok's
    doing so confirmed with `grok inspect` against 1.0.5 — so neither needs anything here.
    """
    if profile.has_agent("copilot"):
        copilot_dir = Path.home() / ".copilot"
        copilot_dir.mkdir(parents=True, exist_ok=True)
        (copilot_dir / "mcp-config.json").write_text(
            json.dumps(build_copilot_mcp_config(paths), indent=2) + "\n", encoding="utf-8"
        )
        _trust_copilot_worktrees(profile, paths)

    for role in profile.roles:
        if role.agent != "codex":
            continue
        worktree = (
            paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)
        )
        home = paths.codex_home(role.role)
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            build_codex_config_toml(paths, worktree), encoding="utf-8"
        )
        seed_codex_auth(home)


#: Codex keeps its OAuth credentials here, relative to CODEX_HOME.
CODEX_AUTH_FILE = "auth.json"


def real_codex_home() -> Path:
    """The user's own CODEX_HOME — `$CODEX_HOME`, or `~/.codex`."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def seed_codex_auth(home: Path) -> bool:
    """
    Copy the user's Codex credentials into a role's isolated CODEX_HOME. True when copied.

    The isolation exists to protect the user's real `config.toml` from Kiln's per-role trust
    and MCP entries — **not** to isolate their identity. Without this the role's home has no
    `auth.json`, Codex sends an unauthenticated request, and the upstream answers `401
    Unauthorized`. Found live the first time a wrapper-mode Codex role ran: every shipped
    profile had `human-in-the-loop` on Claude, so this path had never been exercised.

    Copied per launch rather than symlinked: a symlink needs privileges on Windows, and a
    fresh copy each launch picks up a token the user's own `codex login` has since refreshed.
    The copy is what Codex refreshes in place during the session, so the credential the run
    uses stays valid even across a long swarm.

    Never raises. A missing source is the ordinary "not logged in" case, and it is the CLI's
    own job to say so clearly — failing the whole launch here would replace a good error
    message with a worse one.
    """
    source = real_codex_home() / CODEX_AUTH_FILE
    if not source.is_file():
        log.warning(
            "no Codex credentials at %s; codex roles will fail to authenticate. "
            "Run `codex login` first.", source,
        )
        return False
    try:
        shutil.copy2(source, home / CODEX_AUTH_FILE)
    except OSError as exc:
        log.warning("could not copy Codex credentials into %s: %s", home, exc)
        return False
    return True


def write_sessions_file(profile: Profile, paths: KilnPaths) -> Path:
    """
    Tab-separated role inventory, read by the status bar and `-Stop`.

    The fifth column is the pane's kind (`agent`, `python`, `inbox`, `dashboard`, `cockpit`),
    and it is here because this is the last point that knows it. Everything downstream reads
    this file rather than the profile -- the terminal dashboard and the web cockpit both do --
    so without it neither can tell a role that reports state from a pane that structurally
    cannot, and both listed `inbox`/`dashboard`/`cockpit` as rows of dashes forever.

    The concrete kind rather than a bare `passive` flag: it costs the same byte count and
    answers "what is this pane" as well as "does it have state".
    """
    lines = [
        f"{index}\t{role.role}\t{role.agent}\t{role.display_name}\t{role.scheduler or 'agent'}"
        for index, role in enumerate(profile.roles, start=1)
    ]
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.sessions_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths.sessions_file


def worktree_for(role: RoleConfig, paths: KilnPaths) -> Path:
    return paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)
