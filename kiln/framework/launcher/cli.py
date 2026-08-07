"""
`kiln` entry point — the launch sequence that bin/kiln.ps1 and bin/kiln.sh used to own.

    python -m launcher.cli --working-dir C:\\path\\to\\project
    python -m launcher.cli --stop
    python -m launcher.cli --list-profiles

Flag names keep their PowerShell spellings as aliases (`-WorkingDir`, `-Profile`, …) so the
shim scripts can forward arguments through unchanged.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import generate, scaffold, stop, workspace
from .commands import build_agent_command, render_posix, render_powershell
from .config import Profile, ProfileError, list_profiles, load_profile
from .paths import KilnPaths
from .templates import TemplateError, check_project_scaffolding, resolve_framework_root
from .terminals import TMUX, WEZTERM, PaneSpec, TerminalError, detect_backend
from .terminals import launch as launch_terminal

log = logging.getLogger("kiln")

REQUIRED_TOOLS = ("git",)


class LaunchError(Exception):
    """Fatal, user-facing launch failure."""


def check_dependencies() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
    if missing:
        raise LaunchError(f"required tool(s) not found on PATH: {', '.join(missing)}")


def build_panes(profile: Profile, paths: KilnPaths, branch: str, backend: str) -> list[PaneSpec]:
    """
    Resolve every role into a launch-ready pane.

    The command is rendered for the shell that will host it: pwsh for the Windows backends,
    sh/zsh for tmux. The command *content* is identical either way — only quoting differs.
    """
    render = render_posix if backend == TMUX else render_powershell
    panes: list[PaneSpec] = []
    for role in profile.roles:
        worktree = workspace.worktree_for(role, paths)
        command = build_agent_command(role, paths, branch)
        panes.append(
            PaneSpec(
                role=role.role,
                name=role.title or role.display_name,
                path=str(worktree),
                cmd=render(command),
                mode=role.mode,
                agent=role.agent,
            )
        )
    return panes


def prepare(profile: Profile, paths: KilnPaths) -> str:
    """Everything that must exist before a single pane opens."""
    # Fail here, with a remedy, rather than deep inside file generation with a bare path.
    check_project_scaffolding(paths)

    workspace.initialize_repo(paths)
    workspace.install_git_hooks(paths)
    workspace.warn_if_kiln_untracked(paths)
    branch = workspace.current_branch(paths)

    workspace.prepare_state_dirs(paths)
    workspace.copy_framework_tools(paths)

    # The message queue's schema is owned by the scheduler package, so there is exactly one
    # definition of it rather than the launcher carrying a second copy.
    sys.path.insert(0, str(paths.python_package_root))
    from scheduler.db import ensure_schema

    ensure_schema(paths.db_path)

    current = profile.current_dir_role
    generate.write_mcp_config(
        paths.project_root,
        paths,
        current.role if current else None,
        branch,
        include_channel=current is not None,
    )
    _copy_root_settings(paths)

    # Worker definitions must exist before worktrees, which copy them in.
    for role in profile.roles:
        generate.write_worker_file(role, paths)

    workspace.prepare_worktrees(profile, paths, branch)
    workspace.prepare_skills(profile, paths)
    workspace.prepare_agent_configs(profile, paths)

    for role in profile.roles:
        generate.write_instructions(role, paths, branch, workspace.worktree_for(role, paths))

    workspace.write_sessions_file(profile, paths)
    return branch


def _copy_root_settings(paths: KilnPaths) -> None:
    template = paths.claude_settings_template
    if not template.is_file():
        return
    target = paths.project_root / ".claude"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, target / "settings.json")
    workspace.write_directory_gitignore(target)


def run_launch(args: argparse.Namespace) -> int:
    project_root = Path(args.working_dir).expanduser().resolve()
    if not project_root.is_dir():
        raise LaunchError(f"working directory does not exist: {project_root}")

    paths = KilnPaths.create(project_root, resolve_framework_root())
    check_dependencies()

    profile = load_profile(paths.project_root, paths.framework_root, args.profile)
    log.info("profile: %s (%d roles)", profile.name, len(profile.roles))

    backend = detect_backend(args.terminal)
    log.info("terminal backend: %s", backend)

    branch = prepare(profile, paths)
    log.info("branch: %s", branch)

    panes = build_panes(profile, paths, branch, backend)
    for pane in panes:
        marker = " [scheduler]" if "role_scheduler" in pane.cmd else ""
        log.info("  %-20s %s%s", pane.role, pane.agent, marker)

    command = launch_terminal(
        backend, panes, profile.layout, paths.project_root, dry_run=args.dry_run
    )
    if args.dry_run:
        print("\n--- would launch ---")
        print(" ".join(str(part) for part in command))
        for pane in panes:
            print(f"\n[{pane.role}] cwd={pane.path}\n  {pane.cmd}")
    return 0


def run_stop(args: argparse.Namespace) -> int:
    project_root = Path(args.working_dir).expanduser().resolve()
    paths = KilnPaths.create(project_root, resolve_framework_root())

    roles: list[str] = []
    if paths.sessions_file.is_file():
        for line in paths.sessions_file.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) > 1:
                roles.append(parts[1])

    stopped = stop.stop_all(roles, dry_run=args.dry_run)
    log.info("stopped %d process(es)", len(stopped))
    return 0


def run_init(args: argparse.Namespace) -> int:
    result = scaffold.scaffold(
        target=args.working_dir,
        framework_root=resolve_framework_root(),
        example=args.example,
        no_git=args.no_git,
    )
    if result.warnings:
        log.warning("completed with %d warning(s)", len(result.warnings))
    return 0


def run_list_profiles(args: argparse.Namespace) -> int:
    project_root = Path(args.working_dir).expanduser().resolve()
    paths = KilnPaths.create(project_root, resolve_framework_root())
    for name, description in list_profiles(paths.project_root, paths.framework_root):
        print(f"{name:20} {description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln", description="Launch a Kiln multi-agent swarm."
    )
    parser.add_argument(
        "--working-dir", "--target", "-WorkingDir", "-Target",
        dest="working_dir", default=".",
        help="project directory (default: current directory)",
    )
    parser.add_argument(
        "--profile", "-Profile", dest="profile", default=None,
        help="profile name (default: the profile named by 'default')",
    )
    parser.add_argument(
        "--terminal", "-Terminal", dest="terminal", default=None,
        help=f"terminal backend: {WEZTERM}, wt, {TMUX} or none",
    )
    parser.add_argument("command", nargs="?", default="",
                        help="'init' to scaffold a new project; omit to launch")
    parser.add_argument("--init", "-Init", dest="init", action="store_true",
                        help="scaffold a new project instead of launching")
    parser.add_argument("--example", "-Example", dest="example", default="",
                        help="seed the scaffold from examples/<name>")
    parser.add_argument("--no-git", "-NoGit", dest="no_git", action="store_true",
                        help="skip git initialisation when scaffolding")
    parser.add_argument("--stop", "-Stop", dest="stop", action="store_true",
                        help="stop a running swarm")
    parser.add_argument("--list-profiles", "-ListProfiles", dest="list_profiles",
                        action="store_true", help="list available profiles and exit")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="show what would be launched without starting anything")
    parser.add_argument("--verbose", "-Debug", dest="verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    # `kiln init <dir>` is accepted alongside `--init`, matching both shells' spellings.
    if args.command and args.command != "init":
        log.error("Unknown argument %r. Did you mean 'init'?", args.command)
        return 1

    try:
        if args.list_profiles:
            return run_list_profiles(args)
        if args.init or args.command == "init":
            return run_init(args)
        if args.stop:
            return run_stop(args)
        return run_launch(args)
    except (
        LaunchError,
        ProfileError,
        TemplateError,
        TerminalError,
        workspace.WorkspaceError,
        scaffold.ScaffoldError,
    ) as exc:
        log.error("Error: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
