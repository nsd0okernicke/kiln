"""
Routing decides which role a completed cycle advances to. A silently wrong answer here
misroutes an entire swarm, so malformed input must raise rather than guess.
"""

from __future__ import annotations

import pytest

from kiln.scheduler.domain import routing

# The framework default, as shipped in kiln/project/constitution/workflow.md today.
LEGACY_TABLE = """\
## Handoff Routing

| Role | Sends to |
| ---- | -------- |
| human-in-the-loop | specifier |
| specifier | coder |
| coder | refactorer |
| refactorer | architect |
| architect | specifier |
"""

# The same table extended with the conditional column this change introduces.
CONDITIONAL_TABLE = """\
## Handoff Routing

| Role | Sends to | When Sender |
| ---- | -------- | ----------- |
| human-in-the-loop | specifier | |
| specifier | coder | |
| specifier | human-in-the-loop | architect |
| coder | refactorer | |
| refactorer | architect | |
| architect | specifier | |
"""


class TestLegacyTableParity:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("specifier", "coder"),
            ("coder", "refactorer"),
            ("refactorer", "architect"),
            ("architect", "specifier"),
        ],
    )
    def test_two_column_rows_resolve(self, role, expected):
        assert routing.parse_routing_table(LEGACY_TABLE).resolve(role) == expected

    def test_hyphenated_role_is_parsed(self):
        # Regression against bin/kiln.ps1:793, whose \\w+ regex silently drops every
        # hyphenated role. That bug is masked today only because its single consumer
        # defaults to "specifier", which happens to be correct for this one role.
        table = routing.parse_routing_table(LEGACY_TABLE)
        assert table.resolve("human-in-the-loop") == "specifier"
        assert "human-in-the-loop" in table.roles()

    def test_header_and_separator_rows_are_not_rules(self):
        table = routing.parse_routing_table(LEGACY_TABLE)
        assert len(table.rules) == 5
        assert "role" not in table.roles()

    def test_sender_is_ignored_when_no_conditional_rules_exist(self):
        table = routing.parse_routing_table(LEGACY_TABLE)
        assert table.resolve("specifier", sender="architect") == "coder"


class TestConditionalRouting:
    def test_matching_sender_beats_the_default_row(self):
        table = routing.parse_routing_table(CONDITIONAL_TABLE)
        assert table.resolve("specifier", sender="architect") == "human-in-the-loop"

    def test_non_matching_sender_falls_back_to_the_default_row(self):
        table = routing.parse_routing_table(CONDITIONAL_TABLE)
        assert table.resolve("specifier", sender="human-in-the-loop") == "coder"

    def test_absent_sender_uses_the_default_row(self):
        table = routing.parse_routing_table(CONDITIONAL_TABLE)
        assert table.resolve("specifier") == "coder"

    def test_precedence_holds_regardless_of_row_order(self):
        # The specific rule wins because it is specific, not because it comes first.
        reordered = CONDITIONAL_TABLE.replace(
            "| specifier | coder | |\n| specifier | human-in-the-loop | architect |",
            "| specifier | human-in-the-loop | architect |\n| specifier | coder | |",
        )
        assert routing.parse_routing_table(reordered).resolve("specifier", "architect") == (
            "human-in-the-loop"
        )

    def test_role_with_only_a_conditional_rule_returns_none_for_other_senders(self):
        table = routing.parse_routing_table(
            "| coder | architect | refactorer |\n"
        )
        assert table.resolve("coder", sender="refactorer") == "architect"
        assert table.resolve("coder", sender="specifier") is None
        assert table.resolve("coder") is None

    def test_several_conditional_rules_for_one_role(self):
        table = routing.parse_routing_table(
            "| coder | architect | refactorer |\n"
            "| coder | specifier | human-in-the-loop |\n"
            "| coder | refactorer | |\n"
        )
        assert table.resolve("coder", "refactorer") == "architect"
        assert table.resolve("coder", "human-in-the-loop") == "specifier"
        assert table.resolve("coder", "architect") == "refactorer"

    def test_blank_conditional_cell_is_a_default_not_a_sender_named_empty(self):
        table = routing.parse_routing_table(CONDITIONAL_TABLE)
        assert all(r.is_default for r in table.rules if r.role == "coder")


class TestNormalisation:
    def test_case_and_padding_are_normalised(self):
        table = routing.parse_routing_table("|  SPECIFIER  |  Coder  |  ARCHITECT  |")
        rule = table.rules[0]
        assert (rule.role, rule.target, rule.when_sender) == ("specifier", "coder", "architect")

    def test_lookup_is_case_insensitive(self):
        table = routing.parse_routing_table(CONDITIONAL_TABLE)
        assert table.resolve("SPECIFIER", sender="Architect") == "human-in-the-loop"


class TestMalformedInput:
    def test_duplicate_default_rule_raises(self):
        with pytest.raises(ValueError, match="duplicate routing rule for 'coder'"):
            routing.parse_routing_table("| coder | refactorer |\n| coder | architect |\n")

    def test_duplicate_conditional_rule_raises(self):
        with pytest.raises(ValueError, match="when sender is 'architect'"):
            routing.parse_routing_table(
                "| coder | refactorer | architect |\n| coder | specifier | architect |\n"
            )

    def test_error_names_both_competing_targets(self):
        with pytest.raises(ValueError) as excinfo:
            routing.parse_routing_table("| coder | refactorer |\n| coder | architect |\n")
        assert "refactorer" in str(excinfo.value) and "architect" in str(excinfo.value)

    def test_same_role_with_distinct_conditions_is_allowed(self):
        table = routing.parse_routing_table(
            "| coder | refactorer | architect |\n| coder | specifier | human-in-the-loop |\n"
        )
        assert len(table.rules) == 2

    @pytest.mark.parametrize(
        "text",
        ["", "no table here", "| onlyonecell |", "| coder | |", "| | refactorer |"],
        ids=["empty", "prose", "single-cell", "blank-target", "blank-role"],
    )
    def test_unusable_rows_are_skipped(self, text):
        assert routing.parse_routing_table(text).rules == ()

    def test_unknown_role_resolves_to_none(self):
        assert routing.parse_routing_table(LEGACY_TABLE).resolve("nobody") is None


class TestSectionScoping:
    def test_only_the_handoff_routing_section_is_read(self):
        markdown = """\
## Priority values

| Level | Meaning |
| ----- | ------- |
| high | urgent |

## Handoff Routing

| Role | Sends to |
| ---- | -------- |
| coder | refactorer |

## Commit Convention

| Prefix | Role |
| ------ | ---- |
| bracket | coder |
"""
        table = routing.parse_routing_table(markdown)
        # Regression against the PowerShell version, which scans the whole document and
        # would read all three tables as routing rules.
        assert table.roles() == ("coder",)
        assert table.resolve("coder") == "refactorer"
        assert table.resolve("high") is None

    def test_subsections_under_the_heading_are_included(self):
        markdown = """\
## Handoff Routing

### Defaults

| coder | refactorer |

## Next Section

| high | urgent |
"""
        table = routing.parse_routing_table(markdown)
        assert table.resolve("coder") == "refactorer"
        assert table.resolve("high") is None

    def test_whole_document_is_scanned_when_the_heading_is_absent(self):
        assert routing.parse_routing_table("| coder | refactorer |").resolve("coder") == (
            "refactorer"
        )

    def test_heading_match_is_case_insensitive(self):
        markdown = "# HANDOFF ROUTING\n\n| coder | refactorer |\n"
        assert routing.parse_routing_table(markdown).resolve("coder") == "refactorer"


class TestFileLoading:
    def test_loads_from_disk(self, tmp_path):
        path = tmp_path / "workflow.md"
        path.write_text(CONDITIONAL_TABLE, encoding="utf-8")
        assert routing.load_routing_table(path).resolve("specifier", "architect") == (
            "human-in-the-loop"
        )

    def test_missing_file_yields_an_empty_table(self, tmp_path):
        # Parity with Read-HandoffRoutingTable, which returns an empty hashtable.
        assert routing.load_routing_table(tmp_path / "absent.md").rules == ()

    def test_directory_path_yields_an_empty_table(self, tmp_path):
        assert routing.load_routing_table(tmp_path).rules == ()

    def _shipped_workflow(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[5]
        path = repo_root / "kiln" / "project" / "constitution" / "workflow.md"
        return path.read_text(encoding="utf-8")

    def test_shipped_workflow_md_carries_the_placeholder(self):
        # The file is injected verbatim into wrapper-mode instructions, so its table is
        # rendered from the profile actually running rather than written by hand.
        assert "{{ROUTING_TABLE}}" in self._shipped_workflow()

    def test_shipped_workflow_md_hardcodes_no_routing_rows(self):
        # The invariant that keeps the two halves of routing from drifting: if someone
        # re-adds a table here, agents start reading rules the scheduler does not follow.
        assert routing.parse_routing_table(self._shipped_workflow()).rules == ()


class TestProfileRouting:
    """
    Routing that a profile carries itself, replacing workflow.md's table.

    There is one `## Handoff Routing` table and `parse_routing_table` raises on a duplicate
    `(role, when_sender)` pair. So one file cannot serve two workflow shapes: `full` needs
    `architect -> specifier` and `harden` needs `architect -> human-in-the-loop`, both as the
    architect's *default* row. The clash is not a misroute, it is a parse failure that takes
    down every profile at once.
    """

    def test_nothing_declared_is_an_empty_table(self):
        assert routing.parse_profile_routing(None).rules == ()
        assert routing.parse_profile_routing({}).rules == ()

    def test_a_plain_target_becomes_a_default_rule(self):
        table = routing.parse_profile_routing({"architect": "human-in-the-loop"})
        assert table.resolve("architect") == "human-in-the-loop"
        assert table.rules[0].when_sender is None

    def test_sender_conditions_are_expressible(self):
        table = routing.parse_profile_routing(
            {"specifier": {"default": "coder", "architect": "human-in-the-loop"}}
        )
        assert table.resolve("specifier") == "coder"
        assert table.resolve("specifier", "architect") == "human-in-the-loop"

    def test_the_two_shapes_that_could_not_coexist_now_can(self):
        # The whole reason this exists.
        full = routing.parse_profile_routing({"architect": "specifier"})
        harden = routing.parse_profile_routing({"architect": "human-in-the-loop"})
        assert full.resolve("architect") == "specifier"
        assert harden.resolve("architect") == "human-in-the-loop"

    def test_role_names_are_normalised_like_the_table_parser(self):
        table = routing.parse_profile_routing({"  Architect  ": "human-in-the-loop"})
        assert table.resolve("architect") == "human-in-the-loop"

    @pytest.mark.parametrize("bad", ["not-an-object", ["a"], 42])
    def test_a_non_object_routing_block_is_rejected(self, bad):
        with pytest.raises(ValueError):
            routing.parse_profile_routing(bad)

    @pytest.mark.parametrize("bad", [{"architect": 7}, {"architect": {"default": ""}}])
    def test_a_missing_or_non_string_target_is_rejected(self, bad):
        with pytest.raises(ValueError):
            routing.parse_profile_routing(bad)


class TestRoutingArgumentRoundTrip:
    """
    The scheduler is a separate process and cannot read the launcher's parsed profile, so
    resolved rules travel to it as command-line arguments.
    """

    def test_a_default_rule_round_trips(self):
        table = routing.parse_profile_routing({"architect": "human-in-the-loop"})
        assert routing.parse_routing_arguments(
            routing.format_routing_rules(table)
        ).rules == table.rules

    def test_a_conditional_rule_round_trips(self):
        table = routing.parse_profile_routing(
            {"specifier": {"default": "coder", "architect": "human-in-the-loop"}}
        )
        assert routing.parse_routing_arguments(
            routing.format_routing_rules(table)
        ).rules == table.rules

    def test_the_wire_format_is_readable(self):
        table = routing.parse_profile_routing({"specifier": {"architect": "human-in-the-loop"}})
        assert routing.format_routing_rules(table) == [
            "specifier=human-in-the-loop:architect"
        ]

    @pytest.mark.parametrize("bad", ["no-equals", "=target", "role="])
    def test_a_malformed_argument_raises_rather_than_being_skipped(self, bad):
        # A dropped rule sends a role's handoff somewhere nobody polls, and the work stops
        # dead with no error anywhere.
        with pytest.raises(ValueError):
            routing.parse_routing_arguments([bad])


class TestRenderRoutingTable:
    """
    workflow.md carries a placeholder instead of a hand-written table, because the file is
    injected verbatim into wrapper-mode instructions and a second copy can drift.
    """

    def test_renders_a_markdown_table_with_a_header(self):
        rendered = routing.render_routing_table(
            routing.parse_profile_routing({"coder": "architect"})
        )
        lines = rendered.splitlines()
        assert lines[0].startswith("| Role |")
        assert lines[1].startswith("| ----")
        assert "| coder | architect |" in rendered

    def test_a_default_row_leaves_the_condition_cell_blank(self):
        rendered = routing.render_routing_table(
            routing.parse_profile_routing({"coder": "architect"})
        )
        assert rendered.strip().endswith("|  |")

    def test_a_conditional_row_carries_its_sender(self):
        rendered = routing.render_routing_table(
            routing.parse_profile_routing({"specifier": {"architect": "human-in-the-loop"}})
        )
        assert "| specifier | human-in-the-loop | architect |" in rendered

    def test_an_empty_table_still_renders_something_readable(self):
        # An agent handed a bare header would read it as "no rows here yet" rather than as
        # a rendering failure; say so instead.
        assert "no routing configured" in routing.render_routing_table(routing.RoutingTable())

    def test_it_round_trips_through_the_markdown_parser(self):
        # The rendered table has to be the same grammar the file parser accepts, or the two
        # halves of routing would silently diverge.
        table = routing.parse_profile_routing(
            {
                "coder": "architect",
                "specifier": {"default": "coder", "architect": "human-in-the-loop"},
            }
        )
        reparsed = routing.parse_routing_table(
            "## Handoff Routing\n\n" + routing.render_routing_table(table)
        )
        assert reparsed.resolve("coder") == "architect"
        assert reparsed.resolve("specifier") == "coder"
        assert reparsed.resolve("specifier", "architect") == "human-in-the-loop"
