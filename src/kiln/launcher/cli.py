"""
`kiln` entry point — the launch sequence that bin/kiln.ps1 and bin/kiln.sh used to own.

    python -m kiln.launcher.cli --working-dir C:\\path\\to\\project
    python -m kiln.launcher.cli --stop
    python -m kiln.launcher.cli --list-profiles

Flag names keep their PowerShell spellings as aliases (`-WorkingDir`, `-Profile`, …) so the
shim scripts can forward arguments through unchanged.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import generate, ports, scaffold, stop, workspace
from .commands import (
    PROXY_CAPABLE_AGENTS,
    PROXY_UPSTREAMS,
    build_agent_command,
    proxy_env,
    render_posix,
    render_powershell,
)
from .config import (
    Profile,
    ProfileError,
    apply_agent_override,
    check_launchable,
    list_profiles,
    load_profile,
)
from .generate import CHANNEL_IMPORT_PROBE, MCP_PYTHON
from .paths import KilnPaths, python_command
from .templates import TemplateError, check_project_scaffolding, resolve_framework_root
from .terminals import TMUX, WEZTERM, WINDOWS_TERMINAL, PaneSpec, TerminalError, detect_backend
from .terminals import launch as launch_terminal

log = logging.getLogger("kiln")

REQUIRED_TOOLS = ("git",)


class LaunchError(Exception):
    """Fatal, user-facing launch failure."""


def check_dependencies() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
    if missing:
        raise LaunchError(f"required tool(s) not found on PATH: {', '.join(missing)}")


def warn_if_channel_unavailable(profile: Profile) -> bool:
    """
    Check that the interpreter `.mcp.json` names can actually import the MCP SDK.

    Wrapper-mode roles receive handoffs through the kiln-channel server, which the *agent CLI*
    spawns as `python <channel.py>` — an interpreter Kiln does not control and never verifies.
    When that import fails the server simply never starts, and the failure is invisible: the
    role cannot receive anything, so it stops working the queue and starts asking its human
    what to do instead. That looks like a confused agent rather than a broken dependency.

    Found live twice — once with no `mcp` installed at all, once with mcp 2.0.0, which moved
    `FastMCP` out of `mcp.server.fastmcp`.

    A warning rather than a hard failure: a swarm whose roles all run on the scheduler needs
    no MCP at all, and even a wrapper swarm is worth launching so the operator can see it.
    Returns True when a warning was emitted.
    """
    wrapper_roles = [role.role for role in profile.roles if not role.uses_scheduler]
    if not wrapper_roles:
        return False

    if not shutil.which(MCP_PYTHON):
        log.warning(
            "%r is not on PATH, so the kiln-channel server cannot start. "
            "Roles %s will be unable to receive handoffs.",
            MCP_PYTHON, ", ".join(wrapper_roles),
        )
        return True

    probe = subprocess.run(
        [MCP_PYTHON, "-c", CHANNEL_IMPORT_PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, check=False, timeout=30,
    )
    if probe.returncode == 0:
        return False

    detail = (probe.stderr or "").strip().splitlines()
    log.warning(
        "the kiln-channel MCP server will not start: %r cannot import the MCP SDK (%s). "
        "Roles %s receive handoffs through it and will silently fall back to asking you "
        "for instructions. Fix with:  %s",
        MCP_PYTHON, detail[-1] if detail else "unknown import error",
        ", ".join(wrapper_roles), mcp_install_hint(),
    )
    return True


def mcp_install_hint() -> str:
    """
    The command that actually installs the MCP SDK for `MCP_PYTHON`.

    A plain `pip install -r` is what this printed before, and on Debian/Ubuntu it fails
    outright: PEP 668 marks the system interpreter externally managed, so pip refuses with
    `error: externally-managed-environment` (confirmed on Ubuntu 24.04). Telling someone to
    run a command that cannot work is worse than saying nothing.

    A virtualenv is not the answer here even though pip suggests one: the agent CLI spawns
    the channel server as a bare interpreter name resolved from *its* PATH, so the SDK has to
    be importable by that interpreter — not by a venv it will never look inside. `--user`
    installs into the same interpreter's user site, which is why it is the flag that works.
    """
    requirements = Path("src") / "kiln" / "mcp_server" / "requirements.txt"
    flags = "" if os.name == "nt" else " --user --break-system-packages"
    return f"{MCP_PYTHON} -m pip install{flags} -r {requirements}"


def _hosts_posix_shell(backend: str) -> bool:
    """
    Whether the pane behind `backend` runs a POSIX shell (bash/zsh) rather than pwsh.

    A function of the *host OS*, not the backend name: WezTerm is cross-platform, so on
    Linux/macOS it hosts a POSIX shell exactly like tmux does, while on Windows it hosts
    pwsh, same as Windows Terminal (which only runs on Windows at all). Keying this off
    backend name alone would send PowerShell syntax into a Linux WezTerm pane's bash/zsh —
    it would echo back as a wall of syntax errors instead of launching anything.

    Only the two backends that genuinely pin a shell are special-cased; everything else
    follows the host OS. `none` used to fall through to the PowerShell branch on every
    platform, so the one backend whose entire job is *printing* the command — the thing the
    README pairs with `--dry-run` for diagnosis — showed Linux users `$env:VAR = '...'`
    commands no shell of theirs could run.
    """
    if backend == TMUX:
        return True
    if backend == WINDOWS_TERMINAL:
        return False
    return os.name != "nt"


def build_panes(
    profile: Profile,
    paths: KilnPaths,
    branch: str,
    backend: str,
    proxy_url: str | None = None,
) -> list[PaneSpec]:
    """
    Resolve every role into a launch-ready pane.

    WezTerm and tmux type the command into a live prompt, which echoes it; Windows Terminal
    passes it as `-Command` and does not. Only the former two need the clearing prefix.
    """
    render = render_posix if _hosts_posix_shell(backend) else render_powershell
    clear = backend in (WEZTERM, TMUX)
    panes: list[PaneSpec] = []
    for role in profile.roles:
        worktree = workspace.worktree_for(role, paths)
        command = build_agent_command(
            role, paths, branch, proxy_url=proxy_url, profile=profile
        )
        panes.append(
            PaneSpec(
                role=role.role,
                name=role.title or role.display_name,
                path=str(worktree),
                cmd=render(command, clear=clear),
                mode=role.mode,
                agent=role.agent,
                passive=role.is_passive,
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
    from kiln.scheduler.infrastructure.persistence.db import ensure_schema

    ensure_schema(paths.db_path)

    current = profile.current_dir_role
    generate.write_mcp_config(
        paths.project_root,
        paths,
        current.role if current else None,
        branch,
        include_channel=generate.channel_is_available(current),
    )
    _copy_root_settings(paths)

    # Worker definitions must exist before worktrees, which copy them in.
    for role in profile.roles:
        generate.write_worker_file(role, paths)

    workspace.prepare_worktrees(profile, paths, branch)
    workspace.prepare_skills(profile, paths)
    workspace.prepare_agent_configs(profile, paths)

    for role in profile.roles:
        generate.write_instructions(
            role, paths, branch, workspace.worktree_for(role, paths), profile
        )

    workspace.write_sessions_file(profile, paths, branch)
    return branch


def _copy_root_settings(paths: KilnPaths) -> None:
    template = paths.claude_settings_template
    if not template.is_file():
        return
    target = paths.project_root / ".claude"
    target.mkdir(parents=True, exist_ok=True)
    workspace.copy_template_file(template, target / "settings.json")
    workspace.write_directory_gitignore(target)


#: Where the capture proxy prefers to listen. Probed upward if taken — see `find_free_port`.
DEFAULT_PROXY_PORT = 8787

#: How many ports above the preferred one to try before giving up.
PROXY_PORT_ATTEMPTS = 20

#: How long to wait for the proxy to accept a connection before calling the launch failed.
PROXY_READY_TIMEOUT_SEC = 10.0


def find_free_port(preferred: int, attempts: int = PROXY_PORT_ATTEMPTS) -> int:
    """
    The first bindable port at or above `preferred`.

    A fixed port breaks as soon as two projects capture at once: the second proxy dies on
    bind, and its roles are still pointed at the first project's proxy, which forwards
    happily and records this swarm's traffic into the other project's store. Agents keep
    working, so nothing surfaces the mistake. Observed live with a proxy left over from an
    earlier session still holding the default port.

    Raises LaunchError rather than falling back to an ephemeral port: a swarm whose proxy
    landed somewhere unpredictable is harder to reason about than one that refused to start.
    The probe itself lives in `ports` because the cockpit needs the same one; the *message*
    stays here, because "launch with --no-proxy" is advice only this caller can give.
    """
    port = ports.first_free_port(preferred, attempts)
    if port is None:
        raise LaunchError(
            f"no free port for the capture proxy in {preferred}-{preferred + attempts - 1}. "
            "Leftover proxies from other projects may be holding them: `kiln --stop` clears "
            "every Kiln process, or launch with --no-kiln.proxy."
        )
    return port


def wait_until_listening(
    port: int, timeout: float = PROXY_READY_TIMEOUT_SEC, host: str = "127.0.0.1"
) -> bool:
    """
    Poll-connect until something answers on `port`, or give up.

    The proxy is spawned detached, so `Popen` returning successfully only means the process
    started — a failure to *bind* happens inside the child and lands in its log. Without this
    check the launch continues and every routed role fails at its first API call, a long way
    from the cause.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def proxy_routes(profile: Profile) -> list[str]:
    """
    `--route` arguments for every role whose backend is not the proxy's default upstream.

    Roles, not backends, are what a path prefix identifies, so a mixed-backend swarm needs
    one route per non-Anthropic role rather than a second proxy on another port.
    """
    return [
        f"--route={role.role}={PROXY_UPSTREAMS[role.agent]}"
        for role in profile.roles
        if role.agent in PROXY_UPSTREAMS and role.agent in PROXY_CAPABLE_AGENTS
    ]


def start_proxy(
    paths: KilnPaths,
    port: int,
    capture_mode: str,
    profile: Profile,
    port_is_explicit: bool = False,
) -> str:
    """
    Launch the capture proxy and return the base URL roles should be pointed at.

    Started as a detached background process rather than a pane: it produces no output worth
    watching, and giving it a pane would change every profile's layout.
    `kiln --stop` finds it by command line, like every other Kiln process.

    Raises LaunchError if it cannot start *or* never begins listening — a swarm that silently
    ran unproxied would produce an empty capture and no explanation, and one pointed at
    somebody else's proxy is worse than that.

    `port_is_explicit` distinguishes `--proxy-port 9000` from the default. An explicitly
    requested port that is busy fails rather than silently drifting to the next one; the
    default probes upward so two projects can run at once without any flag at all.
    """
    log_path = paths.logs_dir / "kiln.proxy.log"
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    # Reclaim this project's own leftovers before looking for a port, so that closing the
    # terminal window -- a normal way to end a swarm, and one that never reaches a detached
    # process -- costs nothing more than a stale listener until the next launch. Restarted
    # rather than reused: this run may have asked for a different --capture mode or a
    # different set of routes, and silently keeping the old ones would be a lie.
    stop.stop_project_proxies(paths.traffic_db)

    port = port if port_is_explicit else find_free_port(port)

    command = [
        python_command(), "-m", "kiln.proxy.server",
        "--db-path", str(paths.traffic_db),
        "--port", str(port),
        "--mode", capture_mode,
        *proxy_routes(profile),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": str(paths.python_package_root),
        "PYTHONIOENCODING": "utf-8",
    }
    # Detached, so the proxy outlives the launcher process that spawned it.
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    try:
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=environment,
                cwd=str(paths.project_root),
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
    except OSError as exc:
        raise LaunchError(f"could not start the capture proxy: {exc}") from exc

    if not wait_until_listening(port):
        raise LaunchError(
            f"the capture proxy did not start listening on port {port}; "
            f"see {log_path}. Launch with --no-proxy to run without capture."
        )

    return f"http://127.0.0.1:{port}"


def run_launch(args: argparse.Namespace) -> int:
    project_root = Path(args.working_dir).expanduser().resolve()
    if not project_root.is_dir():
        raise LaunchError(f"working directory does not exist: {project_root}")

    paths = KilnPaths.create(project_root, resolve_framework_root())
    check_dependencies()

    profile = load_profile(paths.project_root, paths.framework_root, args.profile)
    if args.agent_override:
        profile = apply_agent_override(profile, args.agent_override, args.model_override)
        log.info(
            "agent override: every agent-bearing role runs on %s (model: %s)",
            args.agent_override, args.model_override or "the backend's own default",
        )
    check_launchable(profile)
    log.info("profile: %s (%d roles)", profile.name, len(profile.roles))
    warn_if_channel_unavailable(profile)

    backend = detect_backend(args.terminal)
    log.info("terminal backend: %s", backend)

    branch = prepare(profile, paths)
    log.info("branch: %s", branch)

    proxy_url = None
    if args.proxy and not args.dry_run:
        proxy_url = start_proxy(
            paths, args.proxy_port, args.capture, profile,
            port_is_explicit=args.proxy_port != DEFAULT_PROXY_PORT,
        )
        log.info("capture proxy: %s (%s) -> %s", proxy_url, args.capture, paths.traffic_db)
        routed = [role.role for role in profile.roles if proxy_env(role, proxy_url)]
        log.info("  routing: %s", ", ".join(routed) or "(no proxy-capable roles)")
        unrouted = [
            role.role
            for role in profile.roles
            if not role.is_passive and role.agent not in PROXY_CAPABLE_AGENTS
        ]
        if unrouted:
            # Silence here would look like a capture bug later.
            log.warning(
                "  not routed (no verified base-URL override): %s", ", ".join(unrouted)
            )
    elif args.proxy and args.dry_run:
        proxy_url = f"http://127.0.0.1:{args.proxy_port}"
        log.info("capture proxy: would start on %s", proxy_url)

    panes = build_panes(profile, paths, branch, backend, proxy_url=proxy_url)
    for role in profile.roles:
        if role.is_inbox:
            kind = f"inbox -> {role.watched_role}"  # runs no agent at all
        elif role.is_dashboard:
            kind = "dashboard"  # runs no agent at all, aggregates every role
        elif role.is_cockpit:
            kind = "cockpit (browser)"  # serves one page on 127.0.0.1
        elif role.uses_scheduler:
            kind = f"{role.agent} [scheduler]"
        else:
            kind = role.agent
        log.info("  %-20s %s", role.role, kind)

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
    listed = list_profiles(
        paths.project_root, paths.framework_root, include_fixtures=args.all_profiles
    )
    for name, description in listed:
        print(f"{name:20} {description}")
    if not args.all_profiles:
        hidden = len(
            list_profiles(paths.project_root, paths.framework_root, include_fixtures=True)
        ) - len(listed)
        if hidden:
            print(f"\n({hidden} test fixture(s) hidden; --all-profiles to show them)")
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
        # `-ProfileName` was the PowerShell original's *primary* spelling (`-Profile` was its
        # alias), and it is what the README documented. Dropping it silently broke every
        # existing invocation, so both are accepted.
        "--profile", "-Profile", "-ProfileName", dest="profile", default=None,
        help="profile name (default: the profile named by 'default')",
    )
    parser.add_argument(
        "--terminal", "-Terminal", dest="terminal", default=None,
        help=f"terminal backend: {WEZTERM}, wt, {TMUX} or none",
    )
    parser.add_argument(
        "--agent-override", "-AgentOverride", dest="agent_override", default=None,
        help=(
            "run every agent-bearing role of the chosen profile on this backend instead. "
            "Drops each role's model, since model names are backend-specific -- pass "
            "--model-override to set one"
        ),
    )
    parser.add_argument(
        "--model-override", "-ModelOverride", dest="model_override", default="",
        help="model to use with --agent-override (default: let the backend's CLI choose)",
    )
    parser.add_argument(
        "--all-profiles", "-AllProfiles", dest="all_profiles", action="store_true",
        help="with --list-profiles, include profiles marked as test fixtures",
    )
    parser.add_argument("command", nargs="?", default="",
                        help="'init' to scaffold a new project; 'send', 'inbox' or "
                             "'retry' for the human entry points; omit to launch")
    # `kiln init <dir>` is the form both the README and kiln.sh's own usage block document,
    # but only `command` existed, so argparse rejected the directory as an unrecognised
    # argument and the documented Unix scaffolding invocation could not run at all.
    parser.add_argument("init_target", nargs="?", default="",
                        help="with 'init', the project directory to scaffold "
                             "(equivalent to --working-dir)")
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
    parser.add_argument("--proxy", "-Proxy", dest="proxy",
                        action=argparse.BooleanOptionalAction, default=False,
                        help="route agent API traffic through the local capture proxy "
                             "(claude and codex roles). Off by default. Only metadata is "
                             "recorded unless --capture full is also given")
    parser.add_argument("--proxy-port", dest="proxy_port", type=int,
                        default=DEFAULT_PROXY_PORT,
                        help=f"port for the capture proxy (default: {DEFAULT_PROXY_PORT})")
    parser.add_argument("--capture", dest="capture", choices=["metadata", "full"],
                        default="metadata",
                        help="proxy capture depth: 'metadata' records sizes/model/usage, "
                             "'full' also stores request and response bodies")
    parser.add_argument("--verbose", "-Debug", dest="verbose", action="store_true")
    return parser


#: Human entry points, delegated to the scheduler package rather than parsed here.
SUBCOMMANDS = ("send", "inbox", "retry")


def resolve_queue_context(argv: list[str]) -> list[str]:
    """
    Fill in `--db-path` and `--branch` from the project so a human never types them.

    Branch matters more than it looks: messages are branch-scoped, so an inbox watching the
    wrong branch is indistinguishable from an empty one.
    """
    if "--db-path" in argv:
        return argv

    remaining = list(argv)
    working_dir = "."
    for flag in ("--working-dir", "-WorkingDir"):
        if flag in remaining:
            index = remaining.index(flag)
            working_dir = remaining[index + 1]
            del remaining[index : index + 2]
            break

    paths = KilnPaths.create(
        Path(working_dir).expanduser().resolve(), resolve_framework_root()
    )
    if not paths.db_path.is_file():
        raise LaunchError(
            f"no message queue at {paths.db_path}. Launch the swarm in this project first."
        )

    resolved = [*remaining, "--db-path", str(paths.db_path)]
    if "--branch" not in resolved:
        resolved += ["--branch", workspace.current_branch(paths)]
    return resolved


def run_subcommand(name: str, argv: list[str]) -> int:
    """
    Delegate `kiln send` / `kiln inbox` / `kiln retry` to the scheduler package.

    Intercepted before the main parser rather than added as argparse subparsers: the
    top-level parser exists to accept the PowerShell flag spellings the shims forward
    unchanged, and bolting subparsers onto it changes how those are matched.
    """
    from kiln.scheduler.infrastructure.cli import inbox, retry, send

    handlers = {"send": send.main, "inbox": inbox.main, "retry": retry.main}
    return handlers[name](resolve_queue_context(argv))


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in SUBCOMMANDS:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        try:
            return run_subcommand(raw[0], raw[1:])
        except (LaunchError, ProfileError, TemplateError) as exc:
            log.error("Error: %s", exc)
            return 1

    args = build_parser().parse_args(raw)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    # `kiln init <dir>` is accepted alongside `--init`, matching both shells' spellings.
    if args.command and args.command != "init":
        log.error(
            "Unknown argument %r. Expected 'init', 'send', 'inbox' or 'retry', or "
            "named flags "
            "like -WorkingDir.", args.command,
        )
        return 1

    if args.init_target:
        # Only reachable as `init <dir>`: argparse fills positionals in order, so a first
        # positional that is not "init" has already returned above. The bare directory wins
        # over --working-dir's "." default — `kiln init <dir>` naming one directory and
        # scaffolding another would be indefensible.
        args.working_dir = args.init_target

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
