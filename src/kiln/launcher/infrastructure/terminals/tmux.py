"""
tmux backend (macOS/Linux).

One tmux session per role, mirroring `kiln.sh`. Sessions are detached and each role's
command is delivered with `send-keys`, so the swarm survives the terminal window closing —
the user attaches to whichever role they want to watch.

This is the piece that previously had to be written twice; it now shares all of its
upstream logic (profiles, worktrees, generation, command building) with Windows.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from . import PaneSpec

log = logging.getLogger(__name__)

SESSION_PREFIX = "kiln"

#: Scoped by project to avoid multi-instance collision (Finding 4 in issue #46).
#: The original ``kiln-<role>`` format matched the first project's session when two
#: Kiln instances ran in different directories.
#: Set by ``launch()`` on first call; tests that call ``launch()`` without a
#: ``project_dir`` keep the legacy ``kiln`` prefix.
_project_name: str = ""


def session_name(role: str) -> str:
    prefix = _project_name if _project_name else SESSION_PREFIX
    return f"{prefix}-{role}"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False
    )


def session_exists(role: str) -> bool:
    return _run(["tmux", "has-session", "-t", session_name(role)]).returncode == 0


def build_session_commands(pane: PaneSpec) -> list[list[str]]:
    """
    The tmux invocations that create and populate one role's session.

    `send-keys` rather than passing the command to `new-session` directly: the shell stays
    alive when the agent exits, so the pane keeps its scrollback instead of vanishing —
    matching how the Windows backends behave.
    """
    name = session_name(pane.role)
    return [
        ["tmux", "new-session", "-d", "-s", name, "-c", pane.path],
        ["tmux", "rename-window", "-t", f"{name}:0", pane.name],
        ["tmux", "send-keys", "-t", f"{name}:0", pane.cmd, "Enter"],
    ]


def launch(
    panes: list[PaneSpec],
    layout: dict | None,
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Create one detached session per role. Existing sessions are left untouched.

    ``project_dir`` scopes session names by project name to avoid multi-instance
    collision (Finding 4 in issue #46). Tests that omit it keep the legacy ``kiln``
    prefix for backward compatibility.
    """
    planned: list[str] = []

    _require_tmux(dry_run)

    global _project_name
    if project_dir is not None:
        _project_name = f"{SESSION_PREFIX}-{project_dir.name}"
    else:
        _project_name = SESSION_PREFIX

    for pane in panes:
        if not dry_run and session_exists(pane.role):
            log.info("[%s] tmux session already running; leaving it alone", pane.role)
            continue
        planned.extend(_launch_pane(pane, dry_run))

    if _should_log_attach(dry_run, panes):
        log.info("attach with: tmux attach -t %s", session_name(panes[0].role))
    return planned


def _require_tmux(dry_run: bool) -> None:
    if dry_run or shutil.which("tmux"):
        return
    from . import TerminalError

    raise TerminalError("tmux not found on PATH")


def _should_log_attach(dry_run: bool, panes: list[PaneSpec]) -> bool:
    return not dry_run and bool(panes)


def _launch_pane(pane: PaneSpec, dry_run: bool) -> list[str]:
    planned = []
    for command in build_session_commands(pane):
        planned.append(" ".join(command))
        if dry_run:
            continue
        result = _run(command)
        if result.returncode != 0:
            log.error("[%s] tmux failed: %s", pane.role, result.stderr.strip())
            break
    return planned
