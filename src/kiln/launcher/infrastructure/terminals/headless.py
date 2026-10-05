"""
Headless backend — spawn each pane as a detached background process.

The only backend that does not need a terminal multiplexer at all. Each role's command
is started as an OS-level subprocess, with stdout/stderr captured to
``<project_root>/.kiln/logs/pane-<role>.log``. PIDs are recorded in
``<project_root>/.kiln/pids.json`` for PID-exact stop.

Without this, ``backend=none`` merely logs what would have run — it is impossible to
actually launch a swarm without a multiplexer (WezTerm, tmux, Herdr, or Windows Terminal).
This backend is what enables headless runs, containerised runs (Phase 3), and multiple
swarms without multiple terminal windows.

Manual-mode roles (human-in-the-loop, inbox) are interactive and have no meaning under
headless. ``launch()`` emits a warning for each one and skips it — the cockpit is the
operator's intake surface there. To run a full autonomous cycle, pair headless with
``--auto-approve-spec``, which makes the human-in-the-loop scheduler auto-forward
specifier handoffs to coder.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from . import PaneSpec

log = logging.getLogger(__name__)

#: How long to wait for a spawned process to start before treating a missing PID as a
#: launch failure. The process is started detached, so ``Popen`` returning successfully
#: only means the kernel accepted the fork — not that the process is still alive.
PROCESS_START_WAIT_SEC = 2.0

#: JSON file holding ``{role: pid, ...}`` so stop can find processes by role without
#: scanning every process on the machine.
PIDS_FILENAME = "pids.json"


def pids_path(project_dir: Path) -> Path:
    """Where ``pids.json`` lives, inside the run's state directory."""
    return project_dir / ".kiln" / PIDS_FILENAME


def _mark_started(pid: int, role: str, project_dir: Path) -> None:
    """Add or update one role's PID in ``pids.json``, atomically via temp file."""
    path = pids_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    pids: dict[str, int] = {}
    if path.is_file():
        try:
            pids = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pids = {}
    pids[role] = pid

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pids, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_pids(project_dir: Path) -> dict[str, int]:
    """Read ``pids.json``, returning ``{role: pid}`` or an empty dict."""
    path = pids_path(project_dir)
    if not path.is_file():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_script(pane: PaneSpec, project_dir: Path) -> Path:
    """
    Write the pane command to a temporary script file.

    Only used as a fallback when ``raw_argv`` is not set. The script is written to
    ``<project_root>/.kiln/scripts/`` so it survives the launcher process exiting.
    """
    scripts_dir = project_dir / ".kiln" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        script = scripts_dir / f"{pane.role}.ps1"
        script.write_text(pane.cmd, encoding="utf-8")
    else:
        script = scripts_dir / f"{pane.role}.sh"
        script.write_text("#!/bin/sh\n" + pane.cmd, encoding="utf-8")
        script.chmod(0o755)

    return script


def _spawn_one(pane: PaneSpec, log_path: Path, project_dir: Path) -> int | None:
    """
    Spawn one pane's command as a detached subprocess.

    **Direct spawn path** (when ``pane.raw_argv`` is set): runs the command directly
    with ``pane.env`` merged into the environment. No shell, no script file. This
    avoids Windows command-line length limits and PowerShell execution policy issues.

    **Fallback path**: writes the rendered command to a temp script and executes it
    via the appropriate shell (``powershell -File`` on Windows, ``sh`` on POSIX).

    Returns the PID, or None if the process exited immediately.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    creation_flags = 0
    startupinfo = None
    if os.name == "nt":
        creation_flags = subprocess.DETACHED_PROCESS
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        with log_path.open("ab") as log_file:
            if pane.raw_argv:
                # Direct spawn: no shell, no script file.
                # On Windows, replace python.exe with pythonw.exe to prevent
                # console windows from appearing for background processes.
                argv = list(pane.raw_argv)
                if os.name == "nt" and argv:
                    name = argv[0].lower()
                    if name in ("python", "python.exe"):
                        argv[0] = "pythonw"
                    elif name.endswith("\\python.exe"):
                        argv[0] = name[:-4] + "w.exe"
                env = {**os.environ, **pane.env} if pane.env else None
                proc = subprocess.Popen(
                    argv,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(pane.path) if pane.path else str(project_dir),
                    env=env,
                    creationflags=creation_flags,
                    startupinfo=startupinfo,
                    start_new_session=os.name != "nt",
                )
            else:
                script = _write_script(pane, project_dir)
                if os.name == "nt":
                    shell_argv = [
                        "powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(script),
                    ]
                else:
                    shell_argv = ["sh", str(script)]
                proc = subprocess.Popen(
                    shell_argv,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(pane.path) if pane.path else str(project_dir),
                    creationflags=creation_flags,
                    startupinfo=startupinfo,
                    start_new_session=os.name != "nt",
                )
    except OSError as exc:
        log.error("[%s] could not start: %s", pane.role, exc)
        return None

    try:
        proc.wait(timeout=PROCESS_START_WAIT_SEC)
        log.warning(
            "[%s] exited immediately (rc=%s); see %s",
            pane.role,
            proc.returncode,
            log_path,
        )
        return None
    except subprocess.TimeoutExpired:
        pass

    return proc.pid


def _log_file(role: str, project_dir: Path) -> Path:
    """Pane log path: ``<project_root>/.kiln/logs/pane-<role>.log``."""
    return project_dir / ".kiln" / "logs" / f"pane-{role}.log"


def launch(
    panes: list[PaneSpec],
    layout: dict,
    project_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """
    Start every pane as a detached background process.

    Returns the list of spawned commands (for ``--dry-run`` display).

    Manual-mode agent panes are skipped with a warning: they require a human in a terminal
    and have no meaning without one. Inbox, dashboard, and cockpit panes are passive and
    run fine; scheduler-driven agent roles run fine.
    """
    if dry_run:
        for pane in panes:
            if pane.raw_argv:
                log.info("[%s] would spawn: %s", pane.role, " ".join(pane.raw_argv[:6]) + "...")
            else:
                log.info("[%s] would spawn: %s", pane.role, pane.cmd[:200])
        return [pane.cmd for pane in panes]

    skipped: list[str] = []
    spawned: list[str] = []
    for pane in panes:
        if pane.mode == "manual" and not pane.passive:
            log.warning(
                "[%s] role %r is in manual mode and cannot run headless; skipping. "
                "Use --auto-approve-spec to auto-route its handoffs, or run this role "
                "through a terminal backend (WezTerm, tmux, Herdr).",
                pane.role, pane.role,
            )
            skipped.append(pane.role)
            continue

        log_path = _log_file(pane.role, project_dir)
        pid = _spawn_one(pane, log_path, project_dir)
        if pid is not None:
            _mark_started(pid, pane.role, project_dir)
            log.info("[%s] started (pid %s, log: %s)", pane.role, pid, log_path)
            spawned.append(pane.cmd)

    if skipped:
        log.warning(
            "headless: skipped %d manual-mode role(s): %s. "
            "The cockpit can serve as the operator's intake surface.",
            len(skipped), ", ".join(skipped),
        )

    for handler in logging.getLogger().handlers:
        handler.flush()

    return spawned
