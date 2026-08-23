"""
The pinned status line at the bottom of a scheduler pane.

Nothing here drives a real terminal. `StatusBar` writes to an injected stream, so the exact
escape sequences are asserted directly — which is the only way to catch the failure that
matters: a scrolling region installed wrongly corrupts the pane, and a region never
released outlives the process and leaves the shell behaving strangely.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kiln.scheduler.domain.models import TokenUsage
from kiln.scheduler.infrastructure.terminal import pane_status
from kiln.scheduler.infrastructure.terminal.pane_status import (
    PaneStatus,
    StatusBar,
    format_bar,
    format_tokens,
    paint,
)


class FakeTty(io.StringIO):
    """A stream that claims to be a terminal, so the bar enables itself."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty(monkeypatch):
    # A fixed size keeps the escape-sequence assertions exact.
    monkeypatch.setattr(
        pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(24, 40)
    )
    return FakeTty()


class _Size:
    def __init__(self, lines, columns):
        self.lines, self.columns = lines, columns


class TestBarText:
    def test_shows_the_role_and_state(self):
        rendered = format_bar(PaneStatus(role="specifier", state="working"), 60)
        assert "SPECIFIER" in rendered
        assert "working" in rendered

    def test_is_padded_to_exactly_the_pane_width(self):
        # The background colour is painted across the whole string; a short line would
        # leave the rest of the row unstyled and look broken.
        assert len(format_bar(PaneStatus(role="coder"), 72)) == 72

    def test_is_truncated_to_exactly_the_pane_width(self):
        # A line longer than the pane would wrap into the scrolling region and corrupt it.
        status = PaneStatus(role="coder", detail="x" * 500)
        assert len(format_bar(status, 40)) == 40

    def test_truncation_is_marked(self):
        status = PaneStatus(role="coder", detail="y" * 500)
        assert format_bar(status, 40).endswith("\N{HORIZONTAL ELLIPSIS}")

    def test_counters_appear_once_there_is_something_to_count(self):
        status = PaneStatus(role="coder", cycles=3, cost_usd=1.239, target="refactorer")
        rendered = format_bar(status, 90)
        assert "cycle 3" in rendered
        assert "$1.24" in rendered
        assert "refactorer" in rendered

    def test_a_fresh_scheduler_shows_no_empty_counters(self):
        # 'cycle 0  $0.00  0 tok' is noise before anything has happened.
        rendered = format_bar(PaneStatus(role="coder"), 60)
        assert "cycle" not in rendered
        assert "$" not in rendered
        assert "tok" not in rendered

    def test_tokens_appear_once_there_are_some(self):
        status = PaneStatus(role="coder", tokens=TokenUsage(input_tokens=12_345))
        assert "12.3k tok" in format_bar(status, 90)

    def test_the_bar_shows_the_total_across_kinds(self):
        # The bar has one row, so it shows the sum; the breakdown is the dashboard's job.
        status = PaneStatus(
            role="coder", tokens=TokenUsage(input_tokens=1_000, cache_read_tokens=11_345)
        )
        assert "12.3k tok" in format_bar(status, 90)

    def test_zero_width_does_not_raise(self):
        assert format_bar(PaneStatus(role="coder"), 0) == ""


class TestFormatTokens:
    def test_small_counts_are_exact(self):
        assert format_tokens(850) == "850 tok"

    def test_thousands_are_abbreviated(self):
        # A raw count is up to seven digits and would crowd out the target and detail,
        # which are what an operator is actually watching.
        assert format_tokens(12_345) == "12.3k tok"

    def test_millions_are_abbreviated(self):
        assert format_tokens(2_400_000) == "2.4M tok"

    def test_zero_is_still_formatted(self):
        # The bar hides a zero rather than formatting it, but the dashboard's totals row
        # prints whatever this returns.
        assert format_tokens(0) == "0 tok"


class TestColours:
    def test_each_reported_state_has_its_own_colour(self):
        # Every state role_scheduler passes to set_status must be distinguishable.
        for state in (
            "waiting",
            "receiving",
            "working",
            "retrying",
            "handing-off",
            "blocked",
            "idle",
        ):
            assert state in pane_status.STATE_COLORS_HEX, f"{state} would render as unknown"

    def test_failure_states_are_visually_distinct_from_working_ones(self):
        assert pane_status.style_for("blocked") != pane_status.style_for("working")
        assert pane_status.style_for("halted") != pane_status.style_for("idle")

    def test_failure_states_escalate_rather_than_repeat(self):
        # blocked -> escalated -> halted should read as increasing severity, not one flat
        # "something's wrong" red.
        severities = [pane_status.style_for(s) for s in ("blocked", "escalated", "halted")]
        assert len(set(severities)) == 3

    def test_an_unknown_state_still_renders(self):
        expected = pane_status._hex_to_rgb(pane_status.DEFAULT_COLOR_HEX)
        assert pane_status.style_for("something-new") == expected

    def test_paint_wraps_the_text_and_resets(self):
        rendered = paint(PaneStatus(role="coder", state="working"), 30)
        assert rendered.startswith(pane_status.BOLD)
        assert "\x1b[48;2;47;191;159m" in rendered  # #2fbf9f, working's colour
        assert "\x1b[38;2;0;0;0m" in rendered, "text must stay black on every background"
        assert rendered.endswith(pane_status.RESET_STYLE), "an unreset colour bleeds"

    def test_matches_the_wezterm_tab_bar_palette(self):
        # The whole point of one shared table: read it back the way wezterm.py's Lua does
        # (JSON-exported via build_environment) and confirm nothing was lost in translation.
        from kiln.launcher.infrastructure.terminals import wezterm

        environment = wezterm.build_environment([], {}, Path("/proj"))
        exported = json.loads(environment[wezterm.ENV_STATE_COLORS])
        assert exported == pane_status.STATE_COLORS_HEX


class TestWorkerOutputTint:
    def test_wraps_the_line_in_its_background_and_resets(self):
        rendered = pane_status.tint_worker_output("  \N{HAMMER AND WRENCH} Bash  pytest -q")
        r, g, b = pane_status._hex_to_rgb(pane_status.WORKER_OUTPUT_BG_HEX)
        assert rendered == (
            f"\x1b[48;2;{r};{g};{b}m  \N{HAMMER AND WRENCH} Bash  pytest -q"
            f"{pane_status.RESET_STYLE}"
        )

    def test_does_not_override_foreground(self):
        # A wash behind existing text colour, not a competing highlight -- the worker's own
        # tool-call icons and text colouring must survive untouched.
        assert "38;2;" not in pane_status.tint_worker_output("some line")

    def test_distinct_from_every_state_colour(self):
        # If it happened to match a state colour, worker output could be mistaken for the
        # bar reporting that state.
        assert pane_status.WORKER_OUTPUT_BG_HEX not in pane_status.STATE_COLORS_HEX.values()


class TestInstallation:
    def test_reserves_the_last_row_only(self, tty):
        StatusBar(PaneStatus(role="coder"), stream=tty).start()
        # 24 rows, so the scrolling region must be 1..23.
        assert "\x1b[1;23r" in tty.getvalue()

    def test_region_starts_at_row_one_so_scrollback_survives(self, tty):
        # The whole reason the bar is at the bottom: a region starting below row 1 makes
        # the terminal discard scrolled-off lines instead of keeping them.
        StatusBar(PaneStatus(role="coder"), stream=tty).start()
        assert "\x1b[2;" not in tty.getvalue().split("r")[0]
        assert "\x1b[1;23r" in tty.getvalue()

    def test_header_is_written_inside_the_scrolling_region(self, tty):
        output = tty
        StatusBar(PaneStatus(role="coder"), stream=output).start(["line one", "line two"])
        text = output.getvalue()
        assert "line one" in text
        # The region must be installed before the header, because DECSTBM homes the cursor
        # and would otherwise overwrite what was just printed.
        assert text.index("\x1b[1;23r") < text.index("line one")

    def test_paints_the_bar_on_the_reserved_row(self, tty):
        StatusBar(PaneStatus(role="coder"), stream=tty).start()
        assert "\x1b[24;1H" in tty.getvalue()

    def test_cursor_is_saved_and_restored_around_every_paint(self, tty):
        # Without this the bar would yank the cursor out of the scrolling region and the
        # next line of output would land on the bar itself.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        tty.truncate(0), tty.seek(0)
        bar.update(state="working")
        painted = tty.getvalue()
        assert painted.startswith(pane_status.SAVE_CURSOR)
        assert painted.endswith(pane_status.RESTORE_CURSOR)

    def test_a_tiny_pane_is_left_alone(self, monkeypatch):
        # Reserving a row out of four would be worse than having no bar.
        monkeypatch.setattr(
            pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(4, 40)
        )
        stream = FakeTty()
        StatusBar(PaneStatus(role="coder"), stream=stream).start(["header"])
        assert "\x1b[" not in stream.getvalue()
        assert "header" in stream.getvalue(), "the header must survive without the bar"


class TestDisabled:
    def test_a_non_tty_gets_no_escape_sequences(self):
        # Piped or captured output must stay readable; this is also what keeps the test
        # suite's own captured output clean.
        stream = io.StringIO()
        bar = StatusBar(PaneStatus(role="coder"), stream=stream)
        bar.start(["header"])
        bar.update(state="working")
        bar.close()
        assert "\x1b" not in stream.getvalue()

    def test_the_header_is_still_shown_without_a_bar(self):
        stream = io.StringIO()
        StatusBar(PaneStatus(role="coder"), stream=stream).start(["role: coder"])
        assert "role: coder" in stream.getvalue()

    def test_can_be_disabled_explicitly(self, tty):
        bar = StatusBar(PaneStatus(role="coder"), stream=tty, enabled=False)
        bar.start(["header"])
        assert "\x1b" not in tty.getvalue()

    def test_a_closed_stream_disables_the_bar_instead_of_crashing(self, tty):
        # A pane can go away while the scheduler is still running; decoration must never
        # be the thing that kills it.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        tty.close()
        bar.update(state="working")  # must not raise
        assert bar.enabled is False


class TestRepainting:
    def test_unchanged_state_is_not_repainted(self, tty):
        # A bar that rewrote itself every poll would flicker and fight text selection.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        bar.update(state="starting")
        tty.truncate(0), tty.seek(0)
        bar.refresh()
        assert tty.getvalue() == ""

    def test_a_changed_field_repaints(self, tty):
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        tty.truncate(0), tty.seek(0)
        bar.update(cycles=1)
        assert "cycle 1" in tty.getvalue()

    def test_shrinking_below_the_usable_minimum_stops_repainting(self, tty, monkeypatch):
        # Squashed to four rows, reserving one for the bar would leave almost nothing.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        monkeypatch.setattr(
            pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(3, 40)
        )
        tty.truncate(0), tty.seek(0)
        bar.update(state="working")
        assert tty.getvalue() == ""

    def test_a_resize_reinstalls_the_region(self, tty, monkeypatch):
        # The reserved row moves with the pane; a stale region would put the bar in the
        # middle of the output.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        monkeypatch.setattr(
            pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(40, 100)
        )
        tty.truncate(0), tty.seek(0)
        bar.refresh()
        assert "\x1b[1;39r" in tty.getvalue()
        assert "\x1b[40;1H" in tty.getvalue()

    def test_a_resize_does_a_full_repaint(self, tty, monkeypatch):
        # WezTerm's grid layout can resize an early-created pane multiple times in quick
        # succession (once per later pane split off it) before anything running inside gets
        # to observe an intermediate size. Clearing only the immediately-preceding bar row
        # assumed one clean transition and left stale header/bar text behind whenever more
        # than one resize landed between two `refresh()` calls (observed live: a duplicated
        # banner rule and two status bars in one pane). A full clear-and-redraw on every
        # detected size change is correct regardless of how many resizes actually happened.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start(["Kiln scheduler", "role: coder"])
        monkeypatch.setattr(
            pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(40, 100)
        )
        tty.truncate(0), tty.seek(0)
        bar.refresh()
        output = tty.getvalue()
        assert pane_status.CLEAR_SCREEN in output
        assert "role: coder" in output  # the header is redrawn, not just the bar row

    def test_a_resize_redraws_the_header_exactly_once_per_refresh(self, tty, monkeypatch):
        # A full repaint must not turn into its own source of duplication if refresh() is
        # called again with no further size change.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start(["Kiln scheduler", "role: coder"])
        monkeypatch.setattr(
            pane_status.shutil, "get_terminal_size", lambda fallback=None: _Size(40, 100)
        )
        bar.refresh()
        tty.truncate(0), tty.seek(0)
        bar.refresh()
        assert tty.getvalue() == ""


class TestWindowsVtShim:
    """
    Insurance for a plain conhost.exe. WezTerm and Windows Terminal host the shell through
    ConPTY, which has VT enabled already, so this path is not the normal one — but it must
    never be the thing that kills a scheduler.
    """

    @pytest.mark.skipif(pane_status.os.name != "nt", reason="Windows console API")
    def test_skipped_off_windows(self, monkeypatch):
        # `ctypes.windll` does not exist on Unix at all; the guard must come first.
        import ctypes

        monkeypatch.setattr(pane_status.os, "name", "posix")
        monkeypatch.setattr(
            ctypes.windll.kernel32,
            "GetStdHandle",
            lambda _h: pytest.fail("must not touch the Windows console API"),
        )
        pane_status._enable_windows_vt(io.StringIO())

    @pytest.mark.skipif(pane_status.os.name != "nt", reason="Windows console API")
    def test_sets_the_vt_processing_flag(self, monkeypatch):
        import ctypes

        applied = {}
        monkeypatch.setattr(ctypes.windll.kernel32, "GetStdHandle", lambda _h: 7)
        monkeypatch.setattr(
            ctypes.windll.kernel32,
            "GetConsoleMode",
            lambda _handle, mode: applied.setdefault("read", True) or 1,
        )
        monkeypatch.setattr(
            ctypes.windll.kernel32,
            "SetConsoleMode",
            lambda handle, mode: applied.update(handle=handle, mode=mode) or 1,
        )

        pane_status._enable_windows_vt(io.StringIO())

        assert applied["handle"] == 7
        assert applied["mode"] & 0x0004, "ENABLE_VIRTUAL_TERMINAL_PROCESSING must be set"

    @pytest.mark.skipif(pane_status.os.name != "nt", reason="Windows console API")
    def test_a_failing_console_api_is_swallowed(self, monkeypatch):
        import ctypes

        def explode(_handle):
            raise OSError("no console")

        monkeypatch.setattr(ctypes.windll.kernel32, "GetStdHandle", explode)
        pane_status._enable_windows_vt(io.StringIO())  # must not raise


class TestClose:
    def test_releases_the_scrolling_region(self, tty):
        # Left installed, it outlives the process and the shell prompt underneath behaves
        # strangely long after the scheduler is gone.
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        tty.truncate(0), tty.seek(0)
        bar.close()
        assert pane_status.RESET_REGION in tty.getvalue()

    def test_clears_the_bar_row(self, tty):
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        tty.truncate(0), tty.seek(0)
        bar.close()
        assert pane_status.CLEAR_LINE in tty.getvalue()

    def test_closing_twice_is_harmless(self, tty):
        bar = StatusBar(PaneStatus(role="coder"), stream=tty)
        bar.start()
        bar.close()
        tty.truncate(0), tty.seek(0)
        bar.close()
        assert tty.getvalue() == ""

    def test_closing_a_bar_that_never_started_is_harmless(self, tty):
        StatusBar(PaneStatus(role="coder"), stream=tty).close()
        assert tty.getvalue() == ""
