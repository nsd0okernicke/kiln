"""
Stopping a running swarm.

Ports the `-Stop` branch, which used `Get-CimInstance Win32_Process` — a Windows-only WMI
query. Here process discovery goes through the platform's own tooling on each OS, so one
implementation covers both.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: Command-line fragments identifying a process this swarm started.
#:
#: Every python-backed pane type must appear here. The inbox and dashboard were previously
#: missing, so both survived `kiln --stop` and kept polling the
#: database after the swarm was supposedly down (issue #18) -- which also made the
#: stuck-in-`processing` bug (#19) easy to hit, since stopping mid-cycle is routine.
#:
#: The cockpit HTTP adapter holds a listening socket rather than just polling, which makes
#: leaking
#: it worse than leaking a pane: a surviving cockpit keeps a port bound and keeps offering
#: New Task and Teardown buttons for a swarm that no longer exists.
#:
#: The proxy HTTP adapter is a detached background process rather than a pane, but is started by
#: the same launch and must end with it: a capture proxy left listening would keep relaying
#: traffic for whatever ran next.
#:
#: Note `channel.py` is not a pane at all -- it is the MCP channel server an agent CLI
#: spawns -- so an enumeration over pane types will not produce it. It stays here
#: deliberately.
#:
#: Interactive agent-CLI panes (claude/codex/copilot) are absent on purpose: they are not
#: python processes, `_windows_matches` only ever considers python, and a wrapper session
#: dies with its window.
#:
#: These are a fast path, not the whole test: see `STATE_DIR_FRAGMENTS` for why a list of
#: dotted module paths cannot be the only way a Kiln process is recognised.
KILN_PROCESS_MARKERS = (
    "channel.py",
    "kiln.scheduler.infrastructure.cli.role_scheduler",
    "kiln.scheduler.infrastructure.cli.inbox",
    "kiln.scheduler.infrastructure.cli.dashboard",
    "kiln.cockpit.infrastructure.http.server",
    "kiln.proxy.infrastructure.http.server",
)

#: A `.kiln/` path in the command line, the identity that survives refactoring.
#:
#: Every marker above is a dotted module path, which makes `KILN_PROCESS_MARKERS` a list of
#: names that are *free to change* -- and renaming one silently orphans every process already
#: running under the old name. That is not hypothetical: the move to `src/kiln/` renamed the
#: capture proxy's entry point from `proxy.server`, and a proxy started before the move went
#: on running for days, invisible to every `--stop` after it, holding both its port and the
#: inherited handle that made its own log file undeletable.
#:
#: A process pointed at a `.kiln/` state directory is a Kiln process by construction, whatever
#: its module is called this month. Both separators are listed because the fragment is matched
#: against a command line, which may spell paths either way regardless of the host OS.
STATE_DIR_FRAGMENTS = (".kiln/", ".kiln\\")

#: What the capture proxy's `-m` module is called, across every spelling of it.
#:
#: Deliberately the bare word rather than a dotted path, for the reason above; it is read from
#: the `-m` argument rather than matched anywhere in the line so that a project directory
#: containing the word cannot impersonate a module. Paired with the store path in
#: `_matching_proxies` it stays precise: the dashboard and cockpit are handed the same
#: `traffic.db`, but as `--traffic-db` to read, and neither runs a proxy module.
PROXY_MODULE_FRAGMENT = "proxy"


def _windows_matches() -> list[tuple[int, str]]:
    """(pid, command line) for python processes, via CIM."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return _parse_lines(result.stdout, separator="\t")


def _posix_matches() -> list[tuple[int, str]]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return _parse_lines(result.stdout, separator=None)


def _parse_lines(output: str, separator: str | None) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(separator) if separator else line.partition(" ")
        try:
            matches.append((int(pid_text.strip()), command.strip()))
        except ValueError:
            continue
    return matches


def _module_argument(command: str) -> str:
    """The `-m` module a python command line runs, or "" when invoked as a script."""
    tokens = command.split()
    for index, token in enumerate(tokens[:-1]):
        if token == "-m":
            return tokens[index + 1]
    return ""


def is_kiln_process(command: str) -> bool:
    """
    Whether a command line belongs to Kiln.

    Two independent tests, deliberately OR-ed. The markers catch the current release
    precisely; the state-directory fragment catches everything else that is demonstrably
    Kiln's -- older builds, renamed modules, entry points nobody thought to add to the list
    -- so that `--stop` cannot be defeated by a refactor it predates.
    """
    return any(marker in command for marker in KILN_PROCESS_MARKERS) or any(
        fragment in command for fragment in STATE_DIR_FRAGMENTS
    )


def find_kiln_processes() -> list[tuple[int, str]]:
    """Processes this swarm started, identified by their command line."""
    return [(pid, command) for pid, command in _platform_matches() if is_kiln_process(command)]


def find_project_proxies(traffic_db: Path) -> list[tuple[int, str]]:
    """
    Capture proxies already writing to *this* project's store.

    Matched on the `--db-path` argument rather than on the port, because the port is the
    thing that drifts: a leaked proxy holds 8787, the next launch takes 8788, and the store
    it writes to is the only stable identity either of them has.

    The store alone is not enough to name the process, though -- the dashboard and cockpit are
    handed the same file to read -- so it is paired with `PROXY_MODULE_FRAGMENT` rather than
    with the full dotted module path a leaked proxy is precisely the least likely to still be
    using.
    """
    wanted = str(traffic_db)
    if os.name == "nt":
        wanted = wanted.casefold()
    return _matching_proxies(_platform_matches(), wanted, casefold=os.name == "nt")


def _platform_matches() -> list[tuple[int, str]]:
    return _windows_matches() if os.name == "nt" else _posix_matches()


def _matching_proxies(
    candidates: list[tuple[int, str]], wanted: str, *, casefold: bool
) -> list[tuple[int, str]]:
    return [
        (pid, command)
        for pid, command in candidates
        if PROXY_MODULE_FRAGMENT in _module_argument(command)
        and wanted in (command.casefold() if casefold else command)
    ]


def stop_project_proxies(traffic_db: Path) -> list[int]:
    """
    Stop any proxy left over from a previous run of this project. Returns the pids killed.

    Closing the terminal window is a normal way to end a swarm, and it does not reach the
    proxy: that process is deliberately detached so it survives the launcher, which means it
    survives the window too. Without this, every window-close during a `--proxy` run would
    leak one listener, each subsequent launch would climb to the next port, and after
    `PROXY_PORT_ATTEMPTS` of them the launch would fail outright.

    Scoped to this project's store on purpose. `--stop` is machine-wide by design; starting
    a swarm is not, and it has no business killing another project's capture.
    """
    stopped = []
    for pid, command in find_project_proxies(traffic_db):
        log.info("reclaiming leftover capture proxy pid %s: %s", pid, command[:100])
        if kill_process(pid):
            stopped.append(pid)
    return stopped


def kill_process(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    try:
        os.kill(pid, 15)
        return True
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.warning("could not stop pid %s: %s", pid, exc)
        return False


def kill_tmux_sessions(roles: list[str]) -> int:
    """Tear down any tmux sessions this swarm created."""
    if not shutil.which("tmux"):
        return 0
    from .terminals.tmux import session_name

    stopped = 0
    for role in roles:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", session_name(role)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            stopped += 1
    return stopped


def stop_all(roles: list[str] | None = None, dry_run: bool = False) -> list[int]:
    """Stop every Kiln-started process. Returns the pids acted on."""
    found = find_kiln_processes()
    for pid, command in found:
        log.info("stopping pid %s: %s", pid, command[:100])
        if not dry_run:
            kill_process(pid)

    _stop_tmux_roles(roles, dry_run)

    return [pid for pid, _ in found]


def _stop_tmux_roles(roles: list[str] | None, dry_run: bool) -> None:
    if not roles or dry_run:
        return
    stopped = kill_tmux_sessions(roles)
    if stopped:
        log.info("closed %d tmux session(s)", stopped)
