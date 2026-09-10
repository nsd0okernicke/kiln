"""
Herdr backend (macOS/Linux, Windows preview-beta).

See https://herdr.dev. Pane IDs are workspace-global sequential: ``wN:p1``, ``wN:p2``...
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import PaneSpec, TerminalError

log = logging.getLogger(__name__)

WORKSPACE_PREFIX = "kiln"

KILN_STATE_TO_HERDR = {
    "starting": "working", "waiting": "idle", "idle": "idle",
    "receiving": "working", "working": "working", "delegating": "working",
    "verifying": "working", "approval": "working", "retrying": "working",
    "handoff": "done", "handing-off": "done",
    "blocked": "blocked", "escalated": "blocked", "halted": "blocked",
}


def workspace_label(project_dir: Path) -> str:
    return f"{WORKSPACE_PREFIX}-{project_dir.name}"


def pane_id(ws_id: str, num: int) -> str:
    return f"{ws_id}:p{num}"


def launch(
    panes: list[PaneSpec],
    layout: dict | None,
    project_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """Create a detached Herdr workspace with all role tabs and panes.

    Pane IDs are ``wN:p1``, ``wN:p2`` ... globally sequential. The pane
    counter starts at 1 (workspace create → p1). Each tab create and each
    pane split adds one.
    """
    _require_herdr(dry_run)
    label = workspace_label(project_dir)
    planned: list[str] = []

    ws_id, pc = _create_workspace(label, project_dir, planned, dry_run)

    if layout and layout.get("tabs"):
        for tab_index, tab_def in enumerate(layout["tabs"], start=1):
            members = _tab_members(panes, tab_def)
            if not members:
                continue
            title = _tab_title(tab_def, members)
            if tab_index == 1:
                # Rename tab 1 (created by workspace create with default name "1").
                _rename_tab(ws_id, title, planned, dry_run)
            else:
                pc = _create_tab(ws_id, title, planned, dry_run, pc)
            grid_rows = tab_def.get("gridRows") or 1
            grid_cols = tab_def.get("gridCols") or 1
            is_grid = bool(tab_def.get("gridRows") or tab_def.get("gridCols"))
            if is_grid and grid_rows * grid_cols > 1:
                pc = _grid_panes(ws_id, pc, members, grid_rows, grid_cols, planned, dry_run)
            else:
                pc = _linear_panes(ws_id, pc, members, planned, dry_run)
    else:
        for idx, pane in enumerate(panes):
            if idx == 0:
                _run_in_pane(ws_id, pane_id(ws_id, 1), pane, planned, dry_run)
            else:
                pc = _new_tab_with_pane(ws_id, pc, pane, planned, dry_run)

    log.info("workspace_id=%s worktree=%s", ws_id, label)
    return planned


# ---------------------------------------------------------------------------
# Phase 1: workspace
# ---------------------------------------------------------------------------


def _create_workspace(label: str, project_dir: Path, planned: list[str], dry_run: bool) -> tuple[str, int]:
    cmd = ["herdr", "workspace", "create", "--label", label, "--cwd", str(project_dir)]
    planned.append(" ".join(cmd))
    if dry_run:
        return "dry_run", 1
    result = _run(cmd)
    error = _find_json_error(result.stdout) or _find_json_error(result.stderr)
    if result.returncode != 0 or error:
        raise TerminalError(f"failed to create workspace:\n{error or result.stderr.strip()}")
    ws_id = _ws_id_from_create(result.stdout)
    if not ws_id:
        raise TerminalError(f"No workspace_id:\n{result.stdout.strip()}")
    log.info("workspace %s created (id=%s)", label, ws_id)
    return ws_id, 1


# ---------------------------------------------------------------------------
# Phase 2: tabs and panes
# ---------------------------------------------------------------------------


def _rename_tab(ws_id: str, title: str, planned: list[str], dry_run: bool) -> None:
    """Rename tab 1 (created by workspace create with default name "1").

    ``herdr tab rename`` expects the tab's internal ID (``wV:t1``), not the
    tab number "1". The first tab always gets ``t1`` in the workspace.
    """
    if not title:
        return
    tab_id = f"{ws_id}:t1"
    cmd = ["herdr", "tab", "rename", tab_id, title]
    planned.append(" ".join(cmd))
    if not dry_run:
        result = _run_in_ws(cmd, ws_id)
        if result.returncode != 0:
            log.warning("[herdr] tab rename failed: %s", result.stderr.strip()[:200])


def _create_tab(ws_id: str, title: str,
                planned: list[str], dry_run: bool, pc: int) -> int:
    """Create a new tab. Returns pc+1 (the tab's root pane)."""
    cmd = ["herdr", "tab", "create", "--workspace", ws_id]
    if title:
        cmd += ["--label", title]
    planned.append(" ".join(cmd))
    if not dry_run:
        _run_in_ws(cmd, ws_id)
    return pc + 1


def _linear_panes(ws_id: str, pc: int, members: list[PaneSpec],
                  planned: list[str], dry_run: bool) -> int:
    """Linear layout. ``pc`` is the tab's first/root pane. Returns final pc."""
    if not members:
        return pc
    _run_in_pane(ws_id, pane_id(ws_id, pc), members[0], planned, dry_run)
    for idx, pane in enumerate(members[1:], start=1):
        direction = "down" if idx == 1 and len(members) == 2 else "right"
        pc += 1
        _do_split(ws_id, pane_id(ws_id, pc - 1), direction, planned, dry_run)
        _run_in_pane(ws_id, pane_id(ws_id, pc), pane, planned, dry_run)
    return pc


def _grid_panes(ws_id: str, pc: int, members: list[PaneSpec],
                grid_rows: int, grid_cols: int,
                planned: list[str], dry_run: bool) -> int:
    """Grid layout. ``pc`` is the first pane in this tab. Returns final pc.

    Tracks which pane number occupies each grid cell ``(row, col)`` so that
    right-splits use the correct source pane (the cell to the left in the
    same row) rather than guessing from sequential numbering.
    """
    if not members:
        return pc

    # ``cell[row][col]`` = absolute pane number at that grid position.
    cell: dict[int, dict[int, int]] = {1: {1: pc}}

    _run_in_pane(ws_id, pane_id(ws_id, pc), members[0], planned, dry_run)

    # Down splits for rows 2+ (first column)
    for row in range(2, grid_rows + 1):
        idx = (row - 1) * grid_cols
        if idx < len(members):
            pc += 1
            src = cell[row - 1][1]
            _do_split(ws_id, pane_id(ws_id, src), "down", planned, dry_run)
            _run_in_pane(ws_id, pane_id(ws_id, pc), members[idx], planned, dry_run)
            cell[row] = {1: pc}

    # Right splits for remaining columns
    for col in range(2, grid_cols + 1):
        for row in range(1, grid_rows + 1):
            idx = (row - 1) * grid_cols + (col - 1)
            if idx >= len(members):
                continue
            pc += 1
            src = cell[row][col - 1]  # pane to the left in the same row
            _do_split(ws_id, pane_id(ws_id, src), "right", planned, dry_run)
            _run_in_pane(ws_id, pane_id(ws_id, pc), members[idx], planned, dry_run)
            cell[row][col] = pc
    return pc


def _new_tab_with_pane(ws_id: str, pc: int, pane: PaneSpec,
                       planned: list[str], dry_run: bool) -> int:
    """Create a tab for a single pane role. Returns pc+1."""
    cmd = ["herdr", "tab", "create", "--workspace", ws_id]
    if pane.name:
        cmd += ["--label", pane.name]
    planned.append(" ".join(cmd))
    pc += 1
    _run_in_pane(ws_id, pane_id(ws_id, pc), pane, planned, dry_run)
    return pc


# ---------------------------------------------------------------------------
# Pane actions
# ---------------------------------------------------------------------------


def _run_in_pane(ws_id: str, p_id: str, pane: PaneSpec,
                 planned: list[str], dry_run: bool) -> None:
    """Run a command in a pane and register it as an agent."""
    run_cmd = ["herdr", "pane", "run", p_id, pane.cmd]
    agent_cmd = ["herdr", "pane", "report-agent", p_id,
                 "--source", "kiln", "--agent", f"kiln-{pane.role}", "--state", "working"]
    planned.append(" ".join(run_cmd))
    planned.append(" ".join(agent_cmd))
    if dry_run:
        return
    _run_in_ws(run_cmd, ws_id)
    agent_result = _run_in_ws(agent_cmd, ws_id)
    if agent_result.returncode != 0:
        log.warning("[herdr] report-agent failed for %s: %s", p_id,
                    agent_result.stderr.strip()[:200])


def _do_split(ws_id: str, src: str, direction: str,
              planned: list[str], dry_run: bool) -> None:
    cmd = ["herdr", "pane", "split", src, "--direction", direction]
    planned.append(" ".join(cmd))
    if dry_run:
        return
    result = _run_in_ws(cmd, ws_id)
    if result.returncode != 0:
        log.error("[herdr] split failed: %s\n%s", " ".join(cmd), result.stderr.strip())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tab_members(panes: list[PaneSpec], tab_def: dict) -> list[PaneSpec]:
    return [p for e in (tab_def.get("panes") or [])
            for p in [next((x for x in panes if x.role == e.get("role")), None)] if p]


def _tab_title(tab_def: dict, members: list[PaneSpec]) -> str:
    explicit = tab_def.get("title")
    if explicit:
        return str(explicit)
    names = [p.name for p in members if p]
    return " & ".join(names) if names else ""


def _ws_id_from_create(stdout: str) -> str:
    if not stdout:
        return ""
    try:
        return ((json.loads(stdout).get("result") or {}).get("workspace") or {}).get("workspace_id", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


def _find_json_error(output: str) -> str:
    if not output:
        return ""
    try:
        data = json.loads(output)
        err = data.get("error")
        if err:
            msg = err.get("message", "") or str(err)
            code = err.get("code", "")
            return f"[{code}] {msg}" if code else msg
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return ""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)


def _run_in_ws(args: list[str], ws_id: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERDR_WORKSPACE_ID": ws_id}
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, env=env)


def _require_herdr(dry_run: bool) -> None:
    if dry_run or shutil.which("herdr"):
        return
    raise TerminalError("herdr not found on PATH. Install from https://herdr.dev")
