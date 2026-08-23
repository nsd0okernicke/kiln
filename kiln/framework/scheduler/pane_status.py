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

from .models import TokenUsage

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
BOLD = "\x1b[1m"


def scroll_region(top: int, bottom: int) -> str:
    """DECSTBM. Rows are 1-based and inclusive."""
    return f"\x1b[{top};{bottom}r"


def move_to(row: int, column: int = 1) -> str:
    return f"\x1b[{row};{column}H"


# --- appearance ---------------------------------------------------------------------

#: `#rrggbb`, keyed by the states role_scheduler reports through `set_status`.
#:
#: This is the single source of truth for state colour, full stop -- launcher/terminals/
#: wezterm.py's tab-bar badges read the same table (exported as JSON via an env var, see
#: `build_environment`) rather than keeping a second, hand-copied one. They used to be two
#: independently-maintained palettes that happened to use the same state *names*; in
#: practice they drifted (a role's pane bar and its own tab-bar badge showed different
#: colours for the same state) because nothing forced them to agree on *values*. Now there
#: is exactly one table, and both surfaces render from it.
#:
#: `blocked` / `escalated` / `halted` step from amber-red to pure red so repeated trouble
#: reads as escalating, not as one flat "something's wrong" red.
#:
#: `working` / `delegating` is teal, not the orange it used to be: orange reads as caution,
#: but this is the normal, desired, productive state -- the one an operator most wants to
#: see -- so it should read as calm, positive activity instead. Teal was picked because
#: green (waiting/idle) and blue (receiving) -- the palette's other "calm" hues -- are
#: already spoken for; reusing either would make working indistinguishable from an idle or
#: just-arrived role at a glance.
STATE_COLORS_HEX = {
    "starting": "#8a8a88",
    "waiting": "#5ab363",
    "idle": "#5ab363",
    "receiving": "#7aadff",
    "working": "#2fbf9f",
    "delegating": "#2fbf9f",
    # Same family as working -- still busy, just on the role's own quality gate rather than
    # on the worker.
    "verifying": "#2fbf9f",
    "approval": "#ffdd6a",
    "retrying": "#ffdd6a",
    "handoff": "#ac9aff",
    "handing-off": "#ac9aff",
    "blocked": "#c23b3b",
    "escalated": "#e2451f",
    "halted": "#ff2d2d",
}
DEFAULT_COLOR_HEX = "#8a8a88"

STATE_GLYPH = "\N{BLACK CIRCLE}"


def _hex_to_rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def style_for(state: str) -> tuple[int, int, int]:
    return _hex_to_rgb(STATE_COLORS_HEX.get(state, DEFAULT_COLOR_HEX))


#: A faint background wash, applied per line, for one-shot worker output specifically --
#: the streamed `render_event()` lines in adapters/claude_adapter.py (and future
#: copilot/codex adapters), never the scheduler's own `log.info` lines. The scheduler's
#: log line and the worker's own words were, until now, visually identical plain text
#: sharing one pane; a glance couldn't tell "Kiln talking" from "the agent talking" apart.
#: Background only, no foreground override -- deliberately faint enough to read as a
#: wash behind the existing text colour rather than a competing highlight, since the
#: worker's own tool-call icons and text already carry the visual weight in that stream.
WORKER_OUTPUT_BG_HEX = "#1c2333"


def tint_worker_output(line: str) -> str:
    """Wrap one line of streamed worker output in its background wash."""
    r, g, b = _hex_to_rgb(WORKER_OUTPUT_BG_HEX)
    return f"\x1b[48;2;{r};{g};{b}m{line}{RESET_STYLE}"


@dataclass
class PaneStatus:
    """What the bar reports. Plain data so tests can assert on the rendered string."""

    role: str
    state: str = "starting"
    target: str = ""
    cycles: int = 0
    cost_usd: float = 0.0
    #: Cumulative usage across every cycle this role has run, kept broken down rather than
    #: summed to a single count. The breakdown is the actionable part: a large
    #: `cache_read_tokens` is cheap and healthy, a large `input_tokens` is prompt bloat, and
    #: a total alone cannot tell an operator which one they are looking at. All zero on a
    #: backend that reports no usage, which is why the bar hides the segment entirely.
    tokens: TokenUsage = field(default_factory=TokenUsage)
    detail: str = ""


def format_tokens(total: int) -> str:
    """
    A token count short enough for one bar segment: `850 tok`, `12.3k tok`, `1.2M tok`.

    A raw count is up to seven digits and would crowd out the target and detail on a narrow
    pane, which are what an operator is actually watching.
    """
    if total < 1_000:
        return f"{total} tok"
    if total < 1_000_000:
        return f"{total / 1_000:.1f}k tok"
    return f"{total / 1_000_000:.1f}M tok"


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
    if status.tokens.total:
        # Hidden at zero, like cost: a Codex/Copilot role whose usage could not be read
        # should show nothing rather than assert it spent no tokens. The bar shows the
        # total only -- the breakdown is a dashboard-width thing, not a one-row thing.
        segments.append(format_tokens(status.tokens.total))
    if status.target:
        segments.append(f"\N{RIGHTWARDS ARROW} {status.target}")

    line = "   ".join(segments)
    if status.detail:
        line = f"{line}   {status.detail}"

    if len(line) > width:
        return line[: max(width - 1, 0)] + "\N{HORIZONTAL ELLIPSIS}" if width else ""
    return line.ljust(width)


def paint(status: PaneStatus, width: int) -> str:
    """
    The bar text wrapped in its colours.

    True 24-bit colour, not the 256-colour palette: it's what lets this match the WezTerm
    tab-bar badge's colours exactly rather than the closest available approximation.
    Foreground is always black, matching the tab-bar badges (`Foreground = '#000000'` in
    wezterm.py) -- every background in `STATE_COLORS_HEX` is light/mid enough for that to
    stay readable, and one fixed foreground is one less thing that could drift between the
    two surfaces. Bold gives the bar enough visual weight to read as a status indicator
    rather than a second line of ordinary pane text.
    """
    r, g, b = style_for(status.state)
    body = format_bar(status, width)
    return f"{BOLD}\x1b[48;2;{r};{g};{b}m\x1b[38;2;0;0;0m{body}{RESET_STYLE}"


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
    _header: list[str] = field(default_factory=list, init=False)

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
        self._header = header or []
        self._paint_frame(rows)
        self._installed = True
        self.refresh()

    def _paint_frame(self, rows: int) -> None:
        """Clear the pane, lay out the stored header, and (re)install the scrolling region."""
        self._write(
            CLEAR_SCREEN
            + scroll_region(1, rows - BAR_ROWS)
            + move_to(1)
            + ("\n".join(self._header) + "\n" if self._header else "")
        )

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
            # WezTerm's gui-startup splits panes in sequence, resizing every pane already
            # created each time a new one is split off -- a grid layout resizes an
            # early-created pane once per *later* pane split off it (up to three times for a
            # 2x2 grid's first pane), not just once. Clearing only the immediately-preceding
            # bar row assumed exactly one clean transition; with several resizes landing
            # between two `refresh()` calls, that left stale header/bar text behind anyway
            # (observed live: a duplicated banner rule and two "waiting" bars in one pane).
            # A full clear-and-redraw on every detected size change removes the whole class
            # of bug regardless of how many intermediate resizes happened, at the cost of
            # whatever had already scrolled into the region since `start()` -- normally
            # nothing yet, since these resizes land within the first second or two of
            # startup, well before the first real cycle has anything to say.
            self._rows, self._columns = rows, columns
            self._painted = None
            self._paint_frame(rows)

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
