"""
Handoff format compatibility. A downstream legacy-wrapper role reads these messages as
prose, so the rendered output has to stay byte-compatible with workflow.md's template.
"""

from __future__ import annotations

import pytest
from scheduler import handoff

REAL_MESSAGE = """\
Sender: coder
Handoff: order-intake
Branch: main
Commit: abc123def

════════════════════════════════════════════════════════════════
✓ CODER HANDOFF — 2026-08-07 14:03:11
════════════════════════════════════════════════════════════════
Implemented order creation via TDD.

Next role: refactorer
"""

PING_MESSAGE = """\
Sender: specifier
Handoff: health-check
Branch: main
Commit: deadbeef
Kiln-Ping: true

Trail:
- human-in-the-loop (main)
- specifier (main)

Next role: coder
"""


class TestParsing:
    def test_reads_all_routing_fields(self):
        parsed = handoff.parse_handoff(REAL_MESSAGE)
        assert parsed.sender == "coder"
        assert parsed.handoff == "order-intake"
        assert parsed.branch == "main"
        assert parsed.commit == "abc123def"
        assert parsed.is_ping is False
        assert parsed.is_mergeable is True

    def test_raw_content_is_preserved_verbatim(self):
        assert handoff.parse_handoff(REAL_MESSAGE).raw == REAL_MESSAGE

    def test_missing_fields_become_empty_not_errors(self):
        parsed = handoff.parse_handoff("Sender: coder\n")
        assert parsed.sender == "coder"
        assert parsed.handoff == ""
        assert parsed.commit == ""
        assert parsed.is_mergeable is False

    def test_empty_message_parses(self):
        parsed = handoff.parse_handoff("")
        assert parsed.sender == ""
        assert parsed.is_ping is False

    def test_handoff_name_with_hyphens_and_digits(self):
        assert handoff.parse_handoff("Handoff: order-intake-v2\n").handoff == "order-intake-v2"

    def test_field_values_are_stripped(self):
        assert handoff.parse_handoff("Sender:    coder   \n").sender == "coder"

    def test_only_line_initial_fields_are_read(self):
        # "Sender:" mentioned mid-prose must not override the real header.
        content = "Sender: coder\n\nThe body mentions Sender: refactorer in passing.\n"
        assert handoff.parse_handoff(content).sender == "coder"

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "yes", "1"])
    def test_ping_truthy_values(self, value):
        assert handoff.parse_handoff(f"Kiln-Ping: {value}\n").is_ping is True

    @pytest.mark.parametrize("value", ["false", "no", "0", "", "maybe"])
    def test_ping_falsy_values(self, value):
        assert handoff.parse_handoff(f"Kiln-Ping: {value}\n").is_ping is False


class TestTrail:
    def test_reads_ping_trail(self):
        parsed = handoff.parse_handoff(PING_MESSAGE)
        assert parsed.is_ping is True
        assert parsed.trail == ("human-in-the-loop (main)", "specifier (main)")

    def test_absent_trail_is_empty(self):
        assert handoff.parse_handoff(REAL_MESSAGE).trail == ()

    def test_bullets_elsewhere_are_not_trail_entries(self):
        content = "Sender: coder\n\nNotes:\n- not a trail entry\n"
        assert handoff.parse_handoff(content).trail == ()

    def test_trail_stops_at_the_blank_line(self):
        content = "Trail:\n- a (main)\n\n- b (main)\n"
        assert handoff.parse_trail(content) == ("a (main)",)

    def test_trail_stops_at_the_first_non_bullet_line(self):
        content = "Trail:\n- a (main)\nNext role: coder\n- b (main)\n"
        assert handoff.parse_trail(content) == ("a (main)",)

    def test_blank_lines_before_the_first_entry_are_skipped(self):
        assert handoff.parse_trail("Trail:\n\n- a (main)\n") == ("a (main)",)

    def test_append_adds_this_hop(self):
        assert handoff.append_trail_entry(("a (main)",), "coder", "feature-x") == (
            "a (main)",
            "coder (feature-x)",
        )

    def test_append_to_empty_trail(self):
        assert handoff.append_trail_entry((), "specifier", "main") == ("specifier (main)",)


class TestFormatting:
    def _format(self, **overrides):
        args = {
            "sender": "coder",
            "handoff": "order-intake",
            "branch": "main",
            "commit": "abc123def",
            "summary": "Implemented order creation via TDD.",
            "next_role": "refactorer",
            "timestamp": "2026-08-07 14:03:11",
        }
        args.update(overrides)
        return handoff.format_handoff(**args)

    def test_matches_the_workflow_template(self):
        assert self._format() == REAL_MESSAGE.rstrip("\n")

    def test_round_trips_through_the_parser(self):
        parsed = handoff.parse_handoff(self._format())
        assert (parsed.sender, parsed.handoff, parsed.branch, parsed.commit) == (
            "coder",
            "order-intake",
            "main",
            "abc123def",
        )

    def test_banner_rule_is_64_characters(self):
        # Pinned because a legacy wrapper role reads this banner as a visual delimiter.
        assert len(handoff.SEPARATOR) == 64
        assert set(handoff.SEPARATOR) == {"═"}

    def test_role_name_is_upper_cased_in_the_banner(self):
        assert "✓ HUMAN-IN-THE-LOOP HANDOFF" in self._format(sender="human-in-the-loop")

    def test_next_role_is_stated(self):
        assert self._format(next_role="architect").endswith("Next role: architect")

    def test_escalation_field_is_additive(self):
        message = self._format(escalation=True)
        assert "Kiln-Escalation: true" in message
        # Existing parsers must be unaffected by the extra field.
        parsed = handoff.parse_handoff(message)
        assert parsed.sender == "coder"
        assert parsed.commit == "abc123def"

    def test_normal_handoff_has_no_escalation_field(self):
        assert "Kiln-Escalation" not in self._format()

    def test_ping_handoff_carries_the_trail(self):
        message = self._format(
            sender="specifier",
            ping=True,
            trail=("human-in-the-loop (main)", "specifier (main)"),
        )
        parsed = handoff.parse_handoff(message)
        assert parsed.is_ping is True
        assert parsed.trail == ("human-in-the-loop (main)", "specifier (main)")

    def test_multiline_summary_survives_round_trip(self):
        summary = "Did a thing.\nThen did another thing."
        assert summary in self._format(summary=summary)
