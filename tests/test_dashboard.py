"""
The swarm-wide dashboard: one aggregate view instead of N panes checked one at a time.

Pure rendering functions are tested directly against fixed input data, mirroring how
pane_status.py and inbox.py are tested -- no live terminal, no real clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from scheduler import dashboard, db, handoff

pytestmark = pytest.mark.integration

NOW_UTC = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)
NOW_LOCAL = datetime(2026, 8, 9, 17, 0, 0)


def _status(
    state="working", since=None, cycles=None, cost_usd=None, tokens=None, token_usage=None
):
    status = {"role": "coder", "state": state, "since": since or "2026-08-09T14:59:30Z"}
    if cycles is not None:
        status["cycles"] = cycles
    if cost_usd is not None:
        status["cost_usd"] = cost_usd
    if tokens is not None:
        status["tokens"] = tokens
    if token_usage is not None:
        status["token_usage"] = token_usage
    return status


def _message(
    sender="specifier",
    target="coder",
    summary="did the thing",
    escalation=False,
    created_at="2026-08-09 16:59:00",
):
    content = handoff.format_handoff(
        sender=sender, handoff="pending", branch="main", commit="abc1234",
        summary=summary, next_role=target, timestamp="2026-08-09 16:59:00",
        escalation=escalation,
    )
    return {
        "sender": sender,
        "target": target,
        "status": "queued",
        "content": content,
        "created_at": created_at,
    }


class TestReadSessions:
    def test_parses_tab_separated_rows(self, tmp_path):
        path = tmp_path / "sessions"
        path.write_text(
            "1\tcoder\tclaude\tCoder\n2\tspecifier\tclaude\tSpecifier\n", encoding="utf-8"
        )
        sessions = dashboard.read_sessions(path)
        assert [s.role for s in sessions] == ["coder", "specifier"]
        assert sessions[0].agent == "claude"
        assert sessions[0].display_name == "Coder"

    def test_missing_file_is_an_empty_list(self, tmp_path):
        assert dashboard.read_sessions(tmp_path / "absent") == []

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "sessions"
        path.write_text("not enough columns\n1\tcoder\tclaude\tCoder\n", encoding="utf-8")
        assert [s.role for s in dashboard.read_sessions(path)] == ["coder"]


class TestReadStatus:
    def test_reads_the_json_file(self, tmp_path):
        (tmp_path / "coder.json").write_text(json.dumps(_status()), encoding="utf-8")
        status = dashboard.read_status(tmp_path, "coder")
        assert status["state"] == "working"

    def test_missing_file_is_none(self, tmp_path):
        assert dashboard.read_status(tmp_path, "coder") is None

    def test_malformed_json_is_none_not_a_crash(self, tmp_path):
        (tmp_path / "coder.json").write_text("not json", encoding="utf-8")
        assert dashboard.read_status(tmp_path, "coder") is None


class TestExtractSummary:
    def test_pulls_the_line_after_the_banner(self):
        content = handoff.format_handoff(
            sender="coder", handoff="pending", branch="main", commit="abc",
            summary="Implemented the create-book endpoint.", next_role="refactorer",
            timestamp="2026-08-09 16:00:00",
        )
        assert dashboard.extract_summary(content) == "Implemented the create-book endpoint."

    def test_falls_back_to_the_first_line_for_unrecognised_content(self):
        assert dashboard.extract_summary("just some prose\nmore text") == "just some prose"

    def test_truncates_long_summaries(self):
        result = dashboard.extract_summary("x" * 100, max_chars=20)
        assert len(result) == 20
        assert result.endswith("\N{HORIZONTAL ELLIPSIS}")


class TestAgo:
    def test_seconds(self):
        assert dashboard._ago(NOW_UTC.replace(second=30), NOW_UTC.replace(second=45)) == "15s ago"

    def test_minutes(self):
        assert dashboard._ago(datetime(2026, 8, 9, 14, 55), datetime(2026, 8, 9, 15, 0)) == "5m ago"

    def test_hours(self):
        assert dashboard._ago(datetime(2026, 8, 9, 12, 0), datetime(2026, 8, 9, 15, 0)) == "3h ago"

    def test_days(self):
        assert dashboard._ago(datetime(2026, 8, 7, 15, 0), datetime(2026, 8, 9, 15, 0)) == "2d ago"

    def test_never_negative(self):
        # Clock skew between the writer and this pane must not print "-3s ago".
        assert dashboard._ago(NOW_UTC.replace(second=50), NOW_UTC.replace(second=45)) == "0s ago"


class TestRenderStateGrid:
    def test_includes_every_session_role(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        lines = dashboard.render_state_grid(sessions, {}, {}, NOW_UTC)
        assert any("coder" in line for line in lines)

    def test_a_role_with_no_status_file_shows_placeholders(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        lines = dashboard.render_state_grid(sessions, {}, {}, NOW_UTC)
        row = next(line for line in lines if line.startswith("coder"))
        assert "-" in row

    def test_shows_queue_depth(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        lines = dashboard.render_state_grid(sessions, {}, {"coder": 3}, NOW_UTC)
        row = next(line for line in lines if line.startswith("coder"))
        assert "3" in row

    def test_shows_cycles_and_cost(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": _status(cycles=4, cost_usd=1.5)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        row = next(line for line in lines if "coder" in line)
        assert "4" in row and "$1.50" in row

    def test_shows_tokens(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": _status(tokens=12_345)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        row = next(line for line in lines if "coder" in line)
        assert "12.3k tok" in row

    def test_a_role_reporting_no_usage_shows_a_placeholder_not_zero(self):
        # Codex/Copilot roles whose usage could not be read must not claim they spent
        # nothing -- that is the failure mode this whole column exists to remove.
        sessions = [dashboard.RoleSession("specifier", "codex", "Specifier")]
        statuses = {"specifier": _status(cycles=2)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        row = next(line for line in lines if "specifier" in line)
        assert "tok" not in row


class TestCacheShare:
    def test_is_the_cache_read_fraction_of_all_tokens(self):
        assert dashboard.cache_share({"input": 100, "cache_read": 900}) == pytest.approx(0.9)

    def test_no_breakdown_is_unknown_not_zero(self):
        # A backend that reported nothing has not told us its cache rate is zero.
        assert dashboard.cache_share(None) is None
        assert dashboard.cache_share({}) is None

    def test_an_all_zero_breakdown_is_unknown(self):
        assert dashboard.cache_share({"input": 0, "cache_read": 0}) is None

    def test_no_cache_reads_is_a_real_zero(self):
        assert dashboard.cache_share({"input": 500, "output": 20}) == pytest.approx(0.0)

    def test_the_column_renders_a_percentage(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": _status(tokens=1000, token_usage={"input": 100, "cache_read": 900})}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        assert "90%" in next(line for line in lines if "coder" in line)


class TestTokenBreakdown:
    def test_sums_each_kind_across_roles(self):
        statuses = {
            "coder": _status(token_usage={"input": 10, "cache_read": 100}),
            "refactorer": _status(token_usage={"input": 5, "output": 2}),
        }
        assert dashboard.total_token_usage(statuses) == {
            "input": 15, "cache_read": 100, "output": 2
        }

    def test_roles_without_a_breakdown_are_skipped(self):
        assert dashboard.total_token_usage({"coder": _status()}) == {}

    def test_formats_only_the_kinds_reported(self):
        rendered = dashboard.format_token_breakdown({"input": 1200, "cache_read": 8_800_000})
        assert "in 1.2k" in rendered
        assert "cache-read 8.8M" in rendered
        assert "out" not in rendered

    def test_an_empty_breakdown_renders_nothing(self):
        assert dashboard.format_token_breakdown({}) == ""


class TestRenderTotals:
    def test_sums_cost_cycles_and_tokens_across_roles(self):
        statuses = {
            "coder": _status(cycles=4, cost_usd=1.5, tokens=1000),
            "refactorer": _status(cycles=2, cost_usd=0.5, tokens=500),
        }
        cost, cycles, tokens = dashboard.render_totals(statuses)
        assert cost == pytest.approx(2.0)
        assert cycles == 6
        assert tokens == 1500

    def test_missing_fields_count_as_zero(self):
        statuses = {"coder": _status()}
        assert dashboard.render_totals(statuses) == (0, 0, 0)

    def test_empty_is_zero(self):
        assert dashboard.render_totals({}) == (0, 0, 0)


class TestCostIsPartial:
    def _sessions(self, *pairs):
        return [dashboard.RoleSession(role, agent, role) for role, agent in pairs]

    def test_all_cost_reporting_backends_is_complete(self):
        sessions = self._sessions(("coder", "claude"), ("refactorer", "grok"))
        statuses = {"coder": _status(), "refactorer": _status()}
        assert dashboard.cost_is_partial(sessions, statuses) is False

    def test_a_running_codex_role_makes_it_partial(self):
        sessions = self._sessions(("coder", "claude"), ("specifier", "codex"))
        statuses = {"coder": _status(), "specifier": _status()}
        assert dashboard.cost_is_partial(sessions, statuses) is True

    def test_a_copilot_role_that_never_ran_does_not_count(self):
        # No status file means the role has produced nothing, so the total is not yet
        # missing anything on its account.
        sessions = self._sessions(("coder", "claude"), ("specifier", "copilot"))
        assert dashboard.cost_is_partial(sessions, {"coder": _status()}) is False


class TestRenderActivity:
    def test_shows_recent_messages(self):
        lines = dashboard.render_activity([_message()], NOW_LOCAL, limit=8)
        assert any("specifier" in line and "coder" in line for line in lines)

    def test_respects_the_limit(self):
        messages = [_message() for _ in range(5)]
        lines = dashboard.render_activity(messages, NOW_LOCAL, limit=2)
        # header + "Recent activity" + 2 rows
        assert len([line for line in lines if "specifier" in line]) == 2

    def test_empty_says_so(self):
        lines = dashboard.render_activity([], NOW_LOCAL, limit=8)
        assert any("none yet" in line for line in lines)


class TestRenderEscalations:
    def test_filters_to_escalations_only(self):
        messages = [_message(escalation=False), _message(summary="merge conflict", escalation=True)]
        lines = dashboard.render_escalations(messages, NOW_LOCAL)
        assert any("merge conflict" in line for line in lines)
        assert not any("did the thing" in line for line in lines)

    def test_empty_says_so(self):
        lines = dashboard.render_escalations([_message(escalation=False)], NOW_LOCAL)
        assert any("none" in line for line in lines)


class TestRenderDashboard:
    def _render(self, **overrides):
        kwargs = dict(
            project_name="library-hub-testrun5",
            branch="run1",
            sessions=[dashboard.RoleSession("coder", "claude", "Coder")],
            statuses={"coder": _status(cycles=4, cost_usd=1.5)},
            queue_depth={"coder": 1},
            messages=[_message(escalation=True, summary="merge conflict")],
            now_utc=NOW_UTC,
            now_local=NOW_LOCAL,
        )
        kwargs.update(overrides)
        return dashboard.render_dashboard(**kwargs)

    def test_title_names_the_project_and_branch(self):
        lines = self._render()
        assert "library-hub-testrun5" in lines[0]
        assert "run1" in lines[0]

    def test_the_rule_is_at_least_as_wide_as_the_grid(self):
        # A rule sized to the title alone left the table visibly overhanging its own
        # borders once the TOKENS column widened the grid.
        lines = self._render()
        rule, grid_header = lines[1], lines[2]
        assert len(rule) >= len(grid_header)

    def test_a_cost_reporting_swarm_has_no_partial_marker(self):
        text = "\n".join(self._render())
        assert "partial" not in text

    def test_a_codex_role_marks_the_cost_total_partial(self):
        text = "\n".join(
            self._render(
                sessions=[dashboard.RoleSession("specifier", "codex", "Specifier")],
                statuses={"specifier": _status(cycles=2, tokens=4000)},
            )
        )
        assert "$0.00+" in text
        assert "partial" in text

    def test_includes_every_section(self):
        text = "\n".join(self._render())
        assert "TOTAL COST" in text
        assert "Recent activity" in text
        assert "Escalations" in text

    def test_escalation_count_reflects_the_messages(self):
        text = "\n".join(self._render())
        assert "ESCALATIONS: 1" in text

    def test_is_pure_no_side_effects_on_repeated_calls(self):
        # Same input, same output -- nothing here should depend on real time or I/O.
        assert self._render() == self._render()


class TestSnapshot:
    def test_end_to_end_against_a_real_db_and_status_files(self, tmp_path, db_path):
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "coder.json").write_text(
            json.dumps(_status(cycles=2, cost_usd=0.9)), encoding="utf-8"
        )

        sessions_file = tmp_path / "sessions"
        sessions_file.write_text("1\tcoder\tclaude\tCoder\n", encoding="utf-8")

        db.insert_handoff(db_path, "specifier", "coder", _message()["content"], "main")

        ctx = dashboard.DashboardContext(
            db_path=db_path, branch="main", status_dir=status_dir,
            sessions_file=sessions_file, project_name="proj",
        )
        frame = dashboard.snapshot(ctx)
        text = "\n".join(frame)
        assert "coder" in text
        assert "$0.90" in text


class TestCli:
    def test_once_renders_a_single_frame_and_exits(self, tmp_path, db_path, capsys):
        sessions_file = tmp_path / "sessions"
        sessions_file.write_text("1\tcoder\tclaude\tCoder\n", encoding="utf-8")
        status_dir = tmp_path / "status"
        status_dir.mkdir()

        exit_code = dashboard.main([
            "--db-path", str(db_path), "--branch", "main",
            "--status-dir", str(status_dir), "--sessions-file", str(sessions_file),
            "--project-name", "proj", "--once",
        ])
        assert exit_code == 0
        assert "Kiln Dashboard" in capsys.readouterr().out

    def test_required_arguments(self):
        with pytest.raises(SystemExit):
            dashboard.build_parser().parse_args([])
