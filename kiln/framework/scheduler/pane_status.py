"""
A status line pinned to the bottom of a scheduler pane.

Built on DECSTBM — the VT scrolling region — rather than a second pane or a full-screen
TUI. The pane stays an ordinary terminal, so selection, copy/paste and scrollback all keep
working; only the last row is reserved.

**The bar is at the bottom for a technical reason, not a stylistic one.** A terminal
pushes scrolled-off lines into scrollback only when the scrolling region starts at row 1.
A top bar needs a region starting at row 2, and terminals then *discard* those lines
instead of keeping them — the pane would scroll but retain no history. Bottom bar keeps
the history; top bar silently eats it.

Everything that decides *what* the bar says (`format_bar`, `style_for`) is pure and
tested directly. `StatusBar` is the only part that writes escape sequences, and it
disables itself whenever the stream is not a terminal, so piped or captured output stays
clean.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import TextIO

#: Reserved for the bar itself.
BAR_ROWS = 1

#: Below this, a scrolling region is more annoying than useful.
MIN_USABLE_ROWS = 6

# --- escape sequences ---------------------------------------------------------------

CLEAR_SCREEN = "\x1b[2J\x1b[H"
CLEAR_LINE = "\x1b[2K"
SAVE_CURSOR = "\x1b7"  # DECSC: saves absolute position, unlike CSI s on some terminals
RESTORE_CURSOR = "\x1b8"
RESET_STYLE = "\x1b[0m"
RESET_REGION = "\x1b[r"


def scroll_region(top: int, bottom: int) -> str:
    """DECSTBM. Rows are 1-based and inclusive."""
    return f"\x1b[{top};{bottom}r"


def move_to(row: int, column: int = 1) -> str:
    return f"\x1b[{row};{column}H"


# --- appearance ---------------------------------------------------------------------

#: (background, foreground) as 256-colour indices, keyed by the states role_scheduler
#: reports through `set_status`. Deliberately the same vocabulary as the WezTerm tab-bar
#: colours in launcher/terminals/wezterm.py, so a role reads the same in both places.
STATE_STYLE = {
    "starting": (24, 231),
    "waiting": (238, 252),
    "idle": (238, 252),
    "receiving": (25, 231),
    "working": (130, 231),
    "retrying": (90, 231),
    "handing-off": (30, 231),
    "blocked": (88, 231),
    "escalated": (124, 231),
    "halted": (196, 16),
}
DEFAULT_STYLE = (238, 252)

STATE_GLYPH = "\N{BLACK CIRCLE}"


def style_for(state: str) -> tuple[int, int]:
    return STATE_STYLE.get(state, DEFAULT_STYLE)


@dataclass
class PaneStatus:
    """What the bar reports. Plain data so tests can assert on the rendered string."""

    role: str
    state: str = "starting"
    target: str = ""
    cycles: int = 0
    cost_usd: float = 0.0
    detail: str = ""


def format_bar(status: PaneStatus, width: int) -> str:
    """
    Render the bar's text, padded or truncated to exactly `width` columns.

    Exact width matters: the background colour is painted across the whole string, so a
    short line would leave the rest of the row unstyled and a long one would wrap onto the
    scrolling region and corrupt it.
    """
    segments = [f" {status.role.upper()}", f"{STATE_GLYPH} {status.state}"]
    if status.cycles:
        segments.append(f"cycle {status.cycles}")
    if status.cost_usd:
        segments.append(f"${status.cost_usd:.2f}")
    if status.target:
        segments.append(f"\N{RIGHTWARDS ARROW} {status.target}")

    line = "   ".join(segments)
    if status.detail:
        line = f"{line}   {status.detail}"

    if len(line) > width:
        return line[: max(width - 1, 0)] + "\N{HORIZONTAL ELLIPSIS}" if width else ""
    return line.ljust(width)


def paint(status: PaneStatus, width: int) -> str:
    """The bar text wrapped in its colours."""
    background, foreground = style_for(status.state)
    body = format_bar(status, width)
    return f"\x1b[48;5;{background}m\x1b[38;5;{foreground}m{body}{RESET_STYLE}"


# --- terminal driver ----------------------------------------------------------------

def _enable_windows_vt(stream: TextIO) -> None:
    """
    Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for a Windows console.

    WezTerm and Windows Terminal both host the shell through ConPTY, which has VT on
    already, so this is insurance for a plain conhost.exe rather than the normal path.
    Failure is not interesting: the worst case is a bar that does not render, and the
    scheduler must not die over decoration.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # any failure here is non-fatal by design
        pass


@dataclass
class StatusBar:
    """
    Owns the pane's last row and the scrolling region above it.

    Repaints only when the rendered text or the terminal size actually changes: a bar that
    rewrote itself every poll would flicker, and would fight the user's text selection for
    no benefit.
    """

    status: PaneStatus
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    enabled: bool | None = None
    _installed: bool = field(default=False, init=False)
    _painted: str | None = field(default=None, init=False)
    _rows: int = field(default=0, init=False)
    _columns: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.enabled is None:
            # Piped, redirected or captured output must stay free of escape sequences.
            self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())

    def _size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.lines, size.columns

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):  # closed or detached pane
            self.enabled = False

    def start(self, header: list[str] | None = None) -> None:
        """
        Clear the pane, lay out the header, and reserve the last row.

        DECSTBM homes the cursor, so the region is installed *before* anything is printed
        and the cursor is then placed explicitly — restoring a saved position could land
        below the region, where output would overwrite the bar instead of scrolling.
        """
        if not self.enabled:
            if header:
                self._write("\n".join(header) + "\n")
            return

        rows, columns = self._size()
        if rows < MIN_USABLE_ROWS:
            self.enabled = False
            if header:
                self._write("\n".join(header) + "\n")
            return

        _enable_windows_vt(self.stream)
        self._rows, self._columns = rows, columns
        lines = header or []

        self._write(
            CLEAR_SCREEN
            + scroll_region(1, rows - BAR_ROWS)
            + move_to(1)
            + ("\n".join(lines) + "\n" if lines else "")
        )
        self._installed = True
        self.refresh()

    def update(self, **fields: object) -> None:
        """Set any subset of the status fields and repaint if that changed anything."""
        for name, value in fields.items():
            setattr(self.status, name, value)
        self.refresh()

    def refresh(self) -> None:
        if not self.enabled or not self._installed:
            return

        rows, columns = self._size()
        if (rows, columns) != (self._rows, self._columns):
            # The pane was resized: the region and the bar row both moved.
            if rows < MIN_USABLE_ROWS:
                return
            self._rows, self._columns = rows, columns
            self._painted = None
            self._write(SAVE_CURSOR + scroll_region(1, rows - BAR_ROWS) + RESTORE_CURSOR)

        rendered = paint(self.status, columns)
        if rendered == self._painted:
            return
        self._painted = rendered
        self._write(SAVE_CURSOR + move_to(rows) + CLEAR_LINE + rendered + RESTORE_CURSOR)

    def close(self) -> None:
        """
        Hand the whole pane back.

        Without this the region outlives the scheduler and the shell prompt underneath it
        behaves strangely — the pane looks broken long after the process is gone.
        """
        if not self.enabled or not self._installed:
            return
        self._installed = False
        self._write(RESET_REGION + move_to(self._rows) + CLEAR_LINE + RESET_STYLE)
