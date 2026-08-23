"""
Handoff format compatibility. A downstream legacy-wrapper role reads these messages as
prose, so the rendered output has to stay byte-compatible with workflow.md's template.
"""

from __future__ import annotations

import pytest

from kiln.scheduler.domain import handoff

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

    @pytest.mark.parametrize("value", [
        "(none — human request, no prior commit)",
        "none",
        "n/a",
        "-",
        "TBD",
    ])
    def test_prose_placeholder_commit_is_not_mergeable(self, value):
        # A sender with nothing to merge often writes prose instead of leaving the field
        # empty; feeding that to `git merge` escalated an otherwise healthy cycle.
        assert handoff.parse_handoff(f"Commit: {value}\n").is_mergeable is False

    def test_short_and_full_hashes_are_mergeable(self):
        assert handoff.parse_handoff("Commit: abc123d\n").is_mergeable is True
        assert handoff.parse_handoff(f"Commit: {'a1b2c3d4' * 5}\n").is_mergeable is True

    def test_a_branch_with_no_commit_is_still_mergeable(self):
        """
        Every `human-in-the-loop` intake looks like this -- a person handing over a user story
        has committed nothing. Merging only on a commit meant those messages moved no code,
        and the receiver worked from whatever it last saw.

        Measured over four cycles: the specifier fell 30 commits and 60 files behind `run2`
        and wrote a CAT-2 specification having never seen the CAT-5, LOAN-0 or CAT-2
        implementations. The header named the branch the whole time.
        """
        parsed = handoff.parse_handoff("Sender: human-in-the-loop\nBranch: run2\nCommit:\n")

        assert parsed.is_mergeable is True
        assert parsed.merge_target == "run2"

    def test_a_commit_still_wins_over_the_branch(self):
        # The commit is the precise answer: a branch tip can move on between the sender
        # composing the handoff and the receiver picking it up.
        parsed = handoff.parse_handoff("Branch: run2\nCommit: abc123def\n")

        assert parsed.merge_target == "abc123def"

    def test_a_prose_commit_falls_back_to_the_branch(self):
        # "(none — human request, no prior commit)" must never reach `git merge`, but it also
        # must not cost the receiver the branch it was told about.
        parsed = handoff.parse_handoff("Branch: run2\nCommit: (none — human request)\n")

        assert parsed.merge_target == "run2"

    def test_nothing_to_merge_is_still_nothing(self):
        # A ping carries neither, and must not turn into a merge of something arbitrary.
        assert handoff.parse_handoff("Sender: coder\nPing: true\n").is_mergeable is False

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
