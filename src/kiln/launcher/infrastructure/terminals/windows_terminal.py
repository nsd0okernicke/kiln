"""
Windows Terminal backend.

Unlike WezTerm, `wt.exe` takes its whole layout as command-line arguments, so this module
builds the argument vector directly. Ports Build-WindowsTerminalLayout and its two helpers.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from . import PaneSpec

log = logging.getLogger(__name__)

COLOR_SCHEMES = ("One Half Dark", "Solarized Dark", "Tango Dark", "Campbell Powershell")
TAB_MARKERS = ("[1]", "[2]", "[3]", "[4]")

#: `wt.exe` treats a bare `;` as its own command separator.
SEPARATOR = ";"


def _pane_by_role(panes: list[PaneSpec], role: str) -> PaneSpec | None:
    return next((pane for pane in panes if pane.role == role), None)


def _new_tab_args(pane: PaneSpec, index: int, title: str | None = None) -> list[str]:
    return [
        "--title",
        title or f"{TAB_MARKERS[index % len(TAB_MARKERS)]} {pane.name}",
        "-d",
        pane.path,
        "--colorScheme",
        COLOR_SCHEMES[index % len(COLOR_SCHEMES)],
        "pwsh",
        "-NoExit",
        "-Command",
        pane.cmd,
    ]


def build_tabs_layout(panes: list[PaneSpec]) -> list[str]:
    """One tab per role — the default when a profile declares no layout."""
    args: list[str] = []
    for index, pane in enumerate(panes):
        if index:
            args.append(SEPARATOR)
        args.append("new-tab")
        args += _new_tab_args(pane, index)
    return args


def build_layout(panes: list[PaneSpec], layout: dict | None) -> list[str]:
    """
    Build the full `wt.exe` argument vector.

    Panes within a tab become split-panes; `gridRows`/`gridCols` are approximated with
    `split-pane -H/-V` since Windows Terminal has no true grid primitive.
    """
    if not layout or not layout.get("tabs"):
        return build_tabs_layout(panes)

    args = _build_defined_tabs(panes, layout["tabs"])
    return args or build_tabs_layout(panes)


def _build_defined_tabs(panes: list[PaneSpec], tabs: list[dict]) -> list[str]:
    args: list[str] = []
    for tab_def in tabs:
        members = _tab_members(panes, tab_def)
        if not members:
            continue

        if args:
            args.append(SEPARATOR)
        args += _tab_args(members, tab_def, args.count("new-tab"))

    return args


def _tab_members(panes: list[PaneSpec], tab_def: dict) -> list[PaneSpec]:
    candidates = (_pane_by_role(panes, item.get("role")) for item in tab_def.get("panes") or [])
    return [pane for pane in candidates if pane]


def _tab_args(members: list[PaneSpec], tab_def: dict, tab_index: int) -> list[str]:
    args = ["new-tab", *_new_tab_args(members[0], tab_index, title=tab_def.get("title"))]
    rows = tab_def.get("gridRows") or 1
    for position, pane in enumerate(members[1:], start=1):
        # A grid alternates orientation; a linear row is all vertical splits.
        orientation = "-H" if rows > 1 and position % rows else "-V"
        args += [
            SEPARATOR,
            "split-pane",
            orientation,
            "-d",
            pane.path,
            "pwsh",
            "-NoExit",
            "-Command",
            pane.cmd,
        ]
    return args


def launch(panes: list[PaneSpec], layout: dict | None, dry_run: bool = False) -> list[str]:
    command = ["wt.exe", *build_layout(panes, layout)]
    if dry_run:
        return command

    if not shutil.which("wt.exe") and not shutil.which("wt"):
        from . import TerminalError

        raise TerminalError("wt.exe (Windows Terminal) not found on PATH")

    subprocess.Popen(command)
    return command
