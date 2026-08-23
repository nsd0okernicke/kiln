"""
The cockpit page itself — the parts of it that are mechanically checkable.

A single self-contained HTML file has no build step and no type checker, so the one class of
mistake it invites is a rule reading a custom property that no palette defines: the colour
silently falls back to nothing and an element renders invisible, or unstyled, in one theme
only. That is exactly the kind of claim a test can hold, so it does.

Layout and taste are not tested here, and should not be.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiln.cockpit.infrastructure.http.server import STATIC_DIR

pytestmark = pytest.mark.integration

#: Every theme the switcher offers. `dark` is the base `:root` palette and deliberately has
#: no `[data-theme]` block -- it is expressed by removing the attribute.
THEMES = ("dark", "light", "neon")

#: Tokens an alternate theme must restate. A "light" theme that inherited the dark
#: background because it forgot one of these would be broken in the most visible way
#: possible, and every one of them is a surface a reader looks straight at.
CORE_TOKENS = ("--bg", "--panel", "--line", "--ink", "--dim", "--accent", "--surface")


@pytest.fixture(scope="module")
def page() -> str:
    return (STATIC_DIR / "cockpit.html").read_text(encoding="utf-8")


def _declared_in(page: str, selector: str) -> set[str]:
    """Custom properties declared in one rule block, e.g. `:root` or a theme's block."""
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", page)
    return set(re.findall(r"(--[a-z-]+)\s*:", match.group("body"))) if match else set()


class TestThemeTokens:
    def test_every_token_the_page_reads_is_defined_in_the_base_palette(self, page):
        # The base `:root` set is the fallback for all three themes, so a token missing from
        # it is undefined in whichever theme does not restate it.
        declared = _declared_in(page, ":root")
        used = set(re.findall(r"var\((--[a-z-]+)", page))

        assert not used - declared, (
            f"CSS reads custom properties nothing defines: {sorted(used - declared)}"
        )

    @pytest.mark.parametrize("theme", ["light", "neon"])
    def test_an_alternate_theme_restates_every_core_surface(self, page, theme):
        declared = _declared_in(page, f':root[data-theme="{theme}"]')

        missing = [token for token in CORE_TOKENS if token not in declared]

        assert not missing, f"the {theme} theme inherits {missing} from the dark palette"

    def test_dark_is_the_base_palette_rather_than_a_block(self, page):
        # If `dark` ever grows its own block, `applyTheme` must stop expressing it by
        # deleting the attribute — the two have to agree or the default silently breaks.
        assert ':root[data-theme="dark"]' not in page


class TestThemeSwitcher:
    def test_the_header_offers_every_theme(self, page):
        offered = set(re.findall(r'data-theme-choice="([a-z]+)"', page))

        assert offered == set(THEMES)

    def test_the_stored_preference_is_applied_before_the_page_paints(self, page):
        # A theme applied from the script at the bottom renders one frame of the default
        # palette first, so a light-theme user sees a dark flash on every reload.
        head, _, tail = page.partition("<header>")

        assert "kiln-cockpit-theme" in head, (
            "the saved theme must be applied by the inline script above <header>"
        )
        assert "kiln-cockpit-theme" in tail  # and persisted by the main script

    def test_reading_the_stored_preference_is_guarded(self, page):
        # Private windows and blocked site data throw on `localStorage` *access*, not only
        # on write. An unguarded read there would leave the page blank.
        inline = page.partition("<header>")[0]
        script = inline[inline.rindex("<script>") :]

        assert "try" in script and "catch" in script

    def test_an_unrecognised_stored_theme_cannot_reach_the_dom(self, page):
        # Whatever is in localStorage is attacker-free but not trustworthy — a stale name
        # from an older build must fall back, not stamp an attribute nothing styles.
        inline = page.partition("<header>")[0]

        for theme in THEMES:
            assert f'"{theme}"' in inline


class TestIconography:
    def test_every_use_resolves_to_a_symbol_the_page_defines(self, page):
        # A `<use href="#missing">` renders nothing at all -- no error, no fallback, just a
        # gap where the logo was.
        defined = set(re.findall(r'<symbol id="([^"]+)"', page))
        used = {href.lstrip("#") for href in re.findall(r'<use href="([^"]+)"', page)}

        assert used <= defined, f"<use> points at undefined symbols: {sorted(used - defined)}"

    def test_the_kiln_mark_is_drawn_rather_than_fetched_or_embedded(self, page):
        # Traced from docs/images/logo.png as vector geometry: it inherits `currentColor`,
        # so it re-tints itself per theme, and it stays crisp at any zoom. A PNG data URI
        # would do neither and would dwarf the rest of the file.
        assert '<symbol id="i-kiln"' in page
        assert "data:image" not in page

    def test_the_project_and_branch_each_carry_an_icon(self, page):
        # The two facts an operator re-checks constantly. They used to be one run-on string.
        assert "i-folder" in page and "i-branch" in page


class TestWorkingCardAnimation:
    def test_the_pulse_is_driven_by_a_real_message_state(self, page):
        # Decoration on every card would say nothing; this fires only while a worker
        # subprocess is actually running on that item.
        assert 'card.status === "processing"' in page
        assert ".card.working { animation:" in page

    def test_a_finished_card_never_pulses(self, page):
        # `done` is terminal, and a pulsing Done column would read as work still moving.
        assert 'lane !== "done" ? " working"' in page

    def test_reduced_motion_is_honoured(self, page):
        # An operator surface must not force motion on someone whose OS asked for less.
        reduced = page.partition("@media (prefers-reduced-motion: reduce)")[2]

        assert reduced, "no prefers-reduced-motion block"
        assert "animation: none" in reduced.partition("}")[0] + reduced[:300]

    def test_the_state_survives_without_the_animation(self, page):
        # Colour, not just movement, carries it -- otherwise reduced-motion users lose the
        # signal entirely rather than seeing a calmer version of it.
        reduced = page.partition("@media (prefers-reduced-motion: reduce)")[2][:300]

        assert "border-left-color" in reduced


class TestAgentColours:
    """
    Which backend runs a role, shown as coloured text rather than a vendor mark: the page
    must stay self-contained and offline, so a logo would have to be drawn from memory, and
    a not-quite-right vendor mark is worse than an accurate word.
    """

    #: Every backend `kiln.launcher.domain.profile.VALID_AGENTS` accepts.
    AGENTS = ("claude", "codex", "copilot", "grok")

    def test_every_accepted_backend_has_its_own_colour(self, page):
        from kiln.launcher.domain.profile import VALID_AGENTS

        declared = _declared_in(page, ":root")

        missing = [a for a in VALID_AGENTS if f"--agent-{a}" not in declared]
        assert not missing, f"backends with no colour, so they render unstyled: {missing}"

    @pytest.mark.parametrize("theme", ["light", "neon"])
    def test_each_theme_restates_every_agent_colour(self, page, theme):
        # A dark-tuned agent colour inherited into the light theme is the exact failure the
        # core-surface test already guards for the main palette.
        declared = _declared_in(page, f':root[data-theme="{theme}"]')

        missing = [a for a in self.AGENTS if f"--agent-{a}" not in declared]
        assert not missing, f"the {theme} theme inherits {missing} from the dark palette"

    def test_the_model_is_shown_beside_the_agent(self, page):
        assert '"model"' in page or "role.model" in page


class TestComposer:
    """
    One composer for every outbound message. Two of them for one INSERT is how the browser
    and the CLI would come to disagree about what a handoff is.
    """

    def test_it_offers_a_target_and_a_work_item(self, page):
        assert 'id="send-target"' in page and 'id="send-item"' in page

    def test_the_old_separate_composers_are_gone(self, page):
        # The New task dialog and the Chat panel were both replaced; leftovers would be dead
        # ids that `$()` resolves to null at the first click.
        for stale in ('id="task-dialog"', 'id="chat-text"', 'id="task-send"'):
            assert stale not in page, stale

    def test_the_work_item_sentinel_cannot_collide_with_a_real_name(self, page):
        # `_NAME_RE` requires an alphanumeric first character, so a leading underscore is
        # unreachable for a real work item.
        from kiln.scheduler.domain.status_contract import is_valid_work_item_name

        sentinel = re.search(r'const ITEM_OTHER = "([^"]*)"', page).group(1)

        assert not is_valid_work_item_name(sentinel)

    def test_the_halted_warning_is_keyed_off_the_roles_own_state(self, page):
        # Sending to a halted role is a silent no-op, so the one thing the page must not do
        # is stay quiet about it.
        assert 'role.state === "halted"' in page

    def test_it_sits_inside_the_work_queue_section(self, page):
        queue_section = page.partition("<h2>Work queue</h2>")[2].partition("</section>")[0]

        assert 'id="send-target"' in queue_section
        assert 'id="queue"' in queue_section


class TestOperationalQueue:
    def test_board_lanes_show_worktrees_instead_of_item_counts(self, page):
        board = page.partition("function renderBoard")[2].partition("function renderQueue")[0]

        assert "laneRole.worktree" in board
        assert '" Worktree: "' in board and '"#i-branch"' in board
        assert 'cards.length, "count"' not in board

    def test_lane_title_sits_above_the_worktree_identity(self, page):
        assert ".lane > h3 > span:first-child" in page
        heading_rule = page.partition(".lane > h3 {")[2].partition("}")[0]
        assert "flex-direction: column" in heading_rule

    def test_cards_do_not_show_ambiguous_message_counts(self, page):
        board = page.partition("function renderBoard")[2].partition("function renderQueue")[0]

        assert 'card.cycles + " msgs"' not in board

    def test_technical_metrics_live_in_expanded_details(self, page):
        queue = page.partition("function renderQueue")[2].partition("async function pollLog")[0]

        assert '"Tokens"' in queue and '"Cache share"' in queue
        assert "cell.append(metrics)" in queue

    def test_a_halted_role_is_warned_about_rather_than_blocked(self, page):
        # Queueing work for after the role recovers is legitimate; the composer must not
        # disable itself.
        assert "send-warning" in page
        assert "disabled" not in page.partition("function renderSendWarning")[2][:800]


class TestPageIsSelfContained:
    def test_it_loads_nothing_from_the_network(self, page):
        # The cockpit is served by a stdlib HTTP server on a machine that may well be
        # offline, and it is the surface an operator reaches for when a run is going wrong.
        offenders = re.findall(r'(?:src|href)="(https?:|//)', page)

        assert not offenders, f"the page would fetch remote assets: {offenders}"


def test_the_static_directory_is_where_the_server_looks():
    # `STATIC_DIR` is resolved from the module's own location, so a moved package that left
    # the page behind would 500 on `GET /` rather than fail at import.
    assert (STATIC_DIR / "cockpit.html").is_file()
    assert Path(STATIC_DIR).name == "static"
