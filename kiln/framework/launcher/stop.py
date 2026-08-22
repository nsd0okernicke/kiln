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
#: Every python-backed pane type must appear here. `scheduler.inbox` and
#: `scheduler.dashboard` were missing, so both survived `kiln --stop` and kept polling the
#: database after the swarm was supposedly down (issue #18) -- which also made the
#: stuck-in-`processing` bug (#19) easy to hit, since stopping mid-cycle is routine.
#:
#: `cockpit.server` holds a listening socket rather than just polling, which makes leaking
#: it worse than leaking a pane: a surviving cockpit keeps a port bound and keeps offering
#: New Task and Teardown buttons for a swarm that no longer exists.
#:
#: `proxy.server` is a detached background process rather than a pane, but it is started by
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
KILN_PROCESS_MARKERS = (
    "channel.py",
    "scheduler.role_scheduler",
    "scheduler.inbox",
    "scheduler.dashboard",
    "cockpit.server",
    "proxy.server",
)


def _windows_matches() -> list[tuple[int, str]]:
    """(pid, command line) for python processes, via CIM."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
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


def find_kiln_processes() -> list[tuple[int, str]]:
    """Processes this swarm started, identified by their command line."""
    candidates = _windows_matches() if os.name == "nt" else _posix_matches()
    return [
        (pid, command)
        for pid, command in candidates
        if any(marker in command for marker in KILN_PROCESS_MARKERS)
    ]


def find_project_proxies(traffic_db: Path) -> list[tuple[int, str]]:
    """
    Capture proxies already writing to *this* project's store.

    Matched on the `--db-path` argument rather than on the port, because the port is the
    thing that drifts: a leaked proxy holds 8787, the next launch takes 8788, and the store
    it writes to is the only stable identity either of them has.
    """
    wanted = str(traffic_db)
    if os.name == "nt":
        wanted = wanted.casefold()
    matches = []
    for pid, command in _windows_matches() if os.name == "nt" else _posix_matches():
        haystack = command.casefold() if os.name == "nt" else command
        if "proxy.server" in command and wanted in haystack:
            matches.append((pid, command))
    return matches


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

    if roles and not dry_run:
        stopped = kill_tmux_sessions(roles)
        if stopped:
            log.info("closed %d tmux session(s)", stopped)

    return [pid for pid, _ in found]
