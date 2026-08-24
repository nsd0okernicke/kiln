"""
Terminal backends.

Each backend takes the same `PaneSpec` list and is responsible only for getting those
commands into panes. Everything upstream — profile parsing, worktrees, command building —
is backend-agnostic, which is what lets Windows and Unix share one implementation instead
of the parallel `kiln.ps1` / `kiln.sh` copies this replaces.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

WEZTERM = "wezterm"
WINDOWS_TERMINAL = "wt"
TMUX = "tmux"
NONE = "none"

VALID_BACKENDS = (WEZTERM, WINDOWS_TERMINAL, TMUX, NONE)


@dataclass(frozen=True)
class PaneSpec:
    """One role's pane, fully resolved and ready to launch."""

    role: str
    name: str
    path: str
    cmd: str
    mode: str = "auto"
    agent: str = "claude"
    #: Runs no agent and reports no state (inbox, dashboard, cockpit). The pane is still
    #: spawned; this only keeps it out of the tab-bar status badges, where a stateless pane
    #: was rendered as `waiting` -- the Lua's fallback for a `manual` role with no status
    #: file, and a claim that was simply untrue of a healthy kiln.cockpit.
    passive: bool = False

    def as_role_data(self) -> dict:
        """Shape consumed by the WezTerm Lua config's `KILN_ROLES_JSON`."""
        return {
            "role": self.role,
            "name": self.name,
            "path": self.path,
            "cmd": self.cmd,
            "mode": self.mode,
            "agent": self.agent,
            "passive": self.passive,
        }


class TerminalError(Exception):
    """The requested backend is unavailable or unknown."""


def detect_backend(requested: str | None = None, env: dict | None = None) -> str:
    """
    Choose a terminal backend.

    Priority: explicit request > KILN_TERMINAL > running inside WezTerm > whatever is
    installed. Mirrors Get-TerminalBackend, plus tmux for Unix.
    """
    import os

    environment = env if env is not None else os.environ

    if requested:
        return requested.lower()
    if environment.get("KILN_TERMINAL"):
        return environment["KILN_TERMINAL"].lower()
    if shutil.which("wezterm"):
        return WEZTERM
    return _platform_backend(os.name)


def _platform_backend(os_name: str) -> str:
    if os_name == "nt":
        return WINDOWS_TERMINAL
    return TMUX if shutil.which("tmux") else NONE


def launch(
    backend: str,
    panes: list[PaneSpec],
    layout: dict,
    project_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """
    Start the swarm on the chosen backend.

    Returns the command that was (or would be) run, so `--dry-run` can show it without
    spawning anything.
    """
    from . import tmux, wezterm, windows_terminal

    if backend == WEZTERM:
        return wezterm.launch(panes, layout, project_dir, dry_run=dry_run)
    if backend == WINDOWS_TERMINAL:
        return windows_terminal.launch(panes, layout, dry_run=dry_run)
    if backend == TMUX:
        return tmux.launch(panes, layout, dry_run=dry_run)
    if backend == NONE:
        for pane in panes:
            log.info("[%s] would run in %s: %s", pane.role, pane.path, pane.cmd)
        return []
    raise TerminalError(
        f"unknown terminal backend {backend!r}; expected one of {', '.join(VALID_BACKENDS)}"
    )
