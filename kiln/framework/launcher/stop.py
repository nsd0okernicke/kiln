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

log = logging.getLogger(__name__)

#: Command-line fragments identifying a process this swarm started.
KILN_PROCESS_MARKERS = (
    "channel.py",
    "scheduler.role_scheduler",
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
