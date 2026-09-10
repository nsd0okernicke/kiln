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
    _require_herdr(dry_run)
    label = workspace_label(project_dir)
    planned: list[str] = []
    ws_id, pc = _create_workspace(label, project_dir, planned, dry_run)

    if layout and layout.get("tabs"):
        pc = _layout_tabs(ws_id, panes, layout, pc, planned, dry_run)
    else:
        pc = _flat_layout(ws_id, panes, pc, planned, dry_run)

    log.info("workspace_id=%s worktree=%s", ws_id, label)
    _open_herdr_tui(dry_run)
    return planned


# ---------------------------------------------------------------------------
# Layout strategies (extracted from launch to reduce its complexity)
# ---------------------------------------------------------------------------


def _layout_tabs(
    ws_id: str, panes: list[PaneSpec], layout: dict,
    pc: int, planned: list[str], dry_run: bool,
) -> int:
    """Process each tab from the layout. Returns final pane count."""
    for tab_index, tab_def in enumerate(layout["tabs"], start=1):
        members = _tab_members(panes, tab_def)
        if not members:
            continue
        title = _tab_title(tab_def, members)
        if tab_index == 1:
            _rename_tab(ws_id, title, planned, dry_run)
        else:
            pc = _create_tab(ws_id, title, planned, dry_run, pc)
        pc = _tab_panes(ws_id, tab_def, members, pc, planned, dry_run)
    return pc


def _tab_panes(
    ws_id: str, tab_def: dict, members: list[PaneSpec],
    pc: int, planned: list[str], dry_run: bool,
) -> int:
    """Create the pane layout for one tab. Returns updated pane count."""
    grid_rows = tab_def.get("gridRows", 1)
    grid_cols = tab_def.get("gridCols", 1)
    is_grid = grid_rows != 1 or grid_cols != 1
    if is_grid and grid_rows * grid_cols > 1:
        return _grid_panes(ws_id, pc, members, grid_rows, grid_cols, planned, dry_run)
    return _linear_panes(ws_id, pc, members, planned, dry_run)


def _flat_layout(
    ws_id: str, panes: list[PaneSpec],
    pc: int, planned: list[str], dry_run: bool,
) -> int:
    """No layout defined: one tab per role. Returns final pane count."""
    for idx, pane in enumerate(panes):
        if idx == 0:
            _run_in_pane(ws_id, pane_id(ws_id, 1), pane, planned, dry_run)
        else:
            pc = _new_tab_with_pane(ws_id, pc, pane, planned, dry_run)
    return pc


# ---------------------------------------------------------------------------
# Phase 1: workspace
# ---------------------------------------------------------------------------


def _create_workspace(
    label: str, project_dir: Path, planned: list[str], dry_run: bool
) -> tuple[str, int]:
    cmd = ["herdr", "workspace", "create", "--label", label, "--cwd", str(project_dir)]
    planned.append(" ".join(cmd))
    if dry_run:
        return "dry_run", 1
    result = _run(cmd)
    _raise_if_workspace_failed(result)
    ws_id = _ws_id_from_create(result.stdout)
    if not ws_id:
        raise TerminalError(f"No workspace_id:\n{result.stdout.strip()}")
    log.info("workspace %s created (id=%s)", label, ws_id)
    return ws_id, 1


def _raise_if_workspace_failed(result: subprocess.CompletedProcess) -> None:
    error = _find_json_error(result.stdout) or _find_json_error(result.stderr)
    if result.returncode != 0 or error:
        raise TerminalError(
            f"failed to create workspace:\n{error or result.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Phase 2: tabs and panes
# ---------------------------------------------------------------------------


def _rename_tab(ws_id: str, title: str, planned: list[str], dry_run: bool) -> None:
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
    cmd = ["herdr", "tab", "create", "--workspace", ws_id]
    if title:
        cmd += ["--label", title]
    planned.append(" ".join(cmd))
    if not dry_run:
        _run_in_ws(cmd, ws_id)
    return pc + 1


def _linear_panes(ws_id: str, pc: int, members: list[PaneSpec],
                  planned: list[str], dry_run: bool) -> int:
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
    if not members:
        return pc
    cell: dict[int, dict[int, int]] = {1: {1: pc}}
    _run_in_pane(ws_id, pane_id(ws_id, pc), members[0], planned, dry_run)
    pc = _grid_down_splits(ws_id, pc, members, grid_rows, grid_cols, cell, planned, dry_run)
    pc = _grid_right_splits(ws_id, pc, members, grid_rows, grid_cols, cell, planned, dry_run)
    return pc


def _grid_down_splits(
    ws_id: str, pc: int, members: list[PaneSpec],
    grid_rows: int, grid_cols: int,
    cell: dict[int, dict[int, int]],
    planned: list[str], dry_run: bool,
) -> int:
    for row in range(2, grid_rows + 1):
        idx = (row - 1) * grid_cols
        if idx < len(members):
            pc += 1
            _do_split(ws_id, pane_id(ws_id, cell[row - 1][1]), "down", planned, dry_run)
            _run_in_pane(ws_id, pane_id(ws_id, pc), members[idx], planned, dry_run)
            cell[row] = {1: pc}
    return pc


def _grid_right_splits(
    ws_id: str, pc: int, members: list[PaneSpec],
    grid_rows: int, grid_cols: int,
    cell: dict[int, dict[int, int]],
    planned: list[str], dry_run: bool,
) -> int:
    for col in range(2, grid_cols + 1):
        for row in range(1, grid_rows + 1):
            idx = (row - 1) * grid_cols + (col - 1)
            if idx >= len(members):
                continue
            pc += 1
            _do_split(ws_id, pane_id(ws_id, cell[row][col - 1]), "right", planned, dry_run)
            _run_in_pane(ws_id, pane_id(ws_id, pc), members[idx], planned, dry_run)
            cell[row][col] = pc
    return pc


def _new_tab_with_pane(ws_id: str, pc: int, pane: PaneSpec,
                       planned: list[str], dry_run: bool) -> int:
    cmd = ["herdr", "tab", "create", "--workspace", ws_id]
    if pane.name:
        cmd += ["--label", pane.name]
    planned.append(" ".join(cmd))
    if not dry_run:
        _run_in_ws(cmd, ws_id)
    pc += 1
    _run_in_pane(ws_id, pane_id(ws_id, pc), pane, planned, dry_run)
    return pc


# ---------------------------------------------------------------------------
# Pane actions
# ---------------------------------------------------------------------------


def _run_in_pane(ws_id: str, p_id: str, pane: PaneSpec,
                 planned: list[str], dry_run: bool) -> None:
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
    by_role = {p.role: p for p in panes}
    result: list[PaneSpec] = []
    for entry in (tab_def.get("panes") or []):
        pane = by_role.get(entry.get("role"))
        if pane:
            result.append(pane)
    return result


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
        data = json.loads(stdout)
        return ((data.get("result") or {}).get("workspace") or {}).get("workspace_id", "")
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


def _open_herdr_tui(dry_run: bool) -> None:
    """Open the Herdr TUI in the current terminal (blocking until detach).

    After creating the workspace, this hands control to the Herdr TUI so the
    user sees their workspace immediately. Detaching (Ctrl+B q) returns to the
    shell.
    """
    if dry_run:
        return
    try:
        subprocess.run(["herdr"], check=False)
    except Exception:
        log.warning("[herdr] failed to open TUI", exc_info=True)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False
    )


def _run_in_ws(args: list[str], ws_id: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERDR_WORKSPACE_ID": ws_id}
    return subprocess.run(
        args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        check=False, env=env,
    )


def _require_herdr(dry_run: bool) -> None:
    if dry_run or shutil.which("herdr"):
        return
    raise TerminalError("herdr not found on PATH. Install from https://herdr.dev")
