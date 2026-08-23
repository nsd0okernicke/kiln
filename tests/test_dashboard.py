"""
The swarm-wide dashboard: one aggregate view instead of N panes checked one at a time.

Pure rendering functions are tested directly against fixed input data, mirroring how
pane_status.py and inbox.py are tested -- no live terminal, no real clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from kiln.scheduler.domain import handoff
from kiln.scheduler.infrastructure.cli import dashboard
from kiln.scheduler.infrastructure.persistence import db

pytestmark = pytest.mark.integration

NOW_UTC = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)
NOW_LOCAL = datetime(2026, 8, 9, 17, 0, 0)


def _status(
    state="working", since=None, cycles=None, cost_usd=None, tokens=None, token_usage=None,
    **extra,
):
    status = {"role": "coder", "state": state, "since": since or "2026-08-09T14:59:30Z"}
    status.update(extra)
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

    def test_the_kind_column_is_read(self, tmp_path):
        path = tmp_path / "sessions"
        path.write_text(
            "1\tcoder\tclaude\tCoder\tpython\n2\tcockpit\tclaude\tCockpit\tcockpit\n",
            encoding="utf-8",
        )

        sessions = dashboard.read_sessions(path)

        assert [s.kind for s in sessions] == ["python", "cockpit"]
        assert [s.passive for s in sessions] == [False, True]

    def test_the_model_column_is_read(self, tmp_path):
        path = tmp_path / "sessions"
        path.write_text(
            "1\thuman-in-the-loop\tclaude\tHuman\tagent\tclaude-sonnet-5\n"
            "2\tcoder\tcopilot\tCoder\tpython\t\n",
            encoding="utf-8",
        )

        sessions = dashboard.read_sessions(path)

        assert [s.model for s in sessions] == ["claude-sonnet-5", ""]

    def test_a_file_without_the_model_column_still_parses(self, tmp_path):
        # Five columns is what a swarm launched one version ago has on disk.
        path = tmp_path / "sessions"
        path.write_text("1\tcoder\tclaude\tCoder\tpython\n", encoding="utf-8")

        assert dashboard.read_sessions(path)[0].model == ""

    def test_a_file_without_the_kind_column_still_parses(self, tmp_path):
        # A swarm launched before the column existed has exactly this file on disk, and the
        # dashboard polls it every two seconds -- an upgrade must not blank the grid.
        path = tmp_path / "sessions"
        path.write_text("1\tcoder\tclaude\tCoder\n", encoding="utf-8")

        sessions = dashboard.read_sessions(path)

        assert sessions[0].kind == dashboard.DEFAULT_KIND
        assert sessions[0].passive is False

    def test_an_empty_kind_column_falls_back_rather_than_becoming_a_kind(self, tmp_path):
        path = tmp_path / "sessions"
        path.write_text("1\tcoder\tclaude\tCoder\t\n", encoding="utf-8")

        assert dashboard.read_sessions(path)[0].kind == dashboard.DEFAULT_KIND

    def test_passive_panes_are_still_returned(self, tmp_path):
        # `run_stop` and the cockpit's teardown both read this file to close tmux sessions.
        # A passive pane filtered out here is one nothing ever tears down.
        path = tmp_path / "sessions"
        path.write_text("1\tinbox\tclaude\tInbox\tinbox\n", encoding="utf-8")

        assert [s.role for s in dashboard.read_sessions(path)] == ["inbox"]


class TestVisibleRoles:
    """
    Which roles earn a row in a state table. One rule, shared by the terminal grid and the
    cockpit's Work Queue, so the two views cannot disagree about which roles exist.
    """

    def _sessions(self):
        return [
            dashboard.RoleSession("human-in-the-loop", "claude", "Human", "agent"),
            dashboard.RoleSession("inbox", "claude", "Inbox", "inbox"),
            dashboard.RoleSession("coder", "claude", "Coder", "python"),
            dashboard.RoleSession("dashboard", "claude", "Dashboard", "dashboard"),
            dashboard.RoleSession("cockpit", "claude", "Cockpit", "cockpit"),
        ]

    def test_it_drops_every_stateless_pane(self):
        visible = dashboard.visible_roles(self._sessions())

        assert [s.role for s in visible] == ["human-in-the-loop", "coder"]

    def test_it_keeps_the_session_objects_themselves(self):
        # Not names: the callers go on to read `.agent` and `.display_name` off these.
        sessions = self._sessions()

        assert dashboard.visible_roles(sessions) == [sessions[0], sessions[2]]

    def test_a_legacy_session_file_hides_nothing(self):
        # Every role defaults to `agent`, so an upgrade cannot silently empty the grid.
        legacy = [dashboard.RoleSession("coder", "claude", "Coder")]

        assert dashboard.visible_roles(legacy) == legacy


class TestPassiveKindsMatchTheLauncher:
    """
    `scheduler` may not import `launcher` -- the dependency runs the other way -- so
    `PASSIVE_KINDS` restates strings that `launcher.config` owns and writes into the
    sessions file. A test may import both, which is what makes the restatement safe rather
    than a copy waiting to drift.
    """

    def test_the_two_definitions_agree(self):
        from launcher import config

        owned_by_the_launcher = {
            config.SCHEDULER_INBOX, config.SCHEDULER_DASHBOARD, config.SCHEDULER_COCKPIT,
        }

        assert owned_by_the_launcher == dashboard.PASSIVE_KINDS

    def test_the_scheduled_kind_is_not_passive(self):
        # `python` roles are the ones that report the most state of all.
        from launcher import config

        assert config.SCHEDULER_PYTHON not in dashboard.PASSIVE_KINDS


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
        earlier, later = NOW_UTC.replace(second=30), NOW_UTC.replace(second=45)

        assert dashboard.format_age(earlier, later) == "15s ago"

    def test_minutes(self):
        earlier, later = datetime(2026, 8, 9, 14, 55), datetime(2026, 8, 9, 15, 0)

        assert dashboard.format_age(earlier, later) == "5m ago"

    def test_hours(self):
        earlier, later = datetime(2026, 8, 9, 12, 0), datetime(2026, 8, 9, 15, 0)

        assert dashboard.format_age(earlier, later) == "3h ago"

    def test_days(self):
        earlier, later = datetime(2026, 8, 7, 15, 0), datetime(2026, 8, 9, 15, 0)

        assert dashboard.format_age(earlier, later) == "2d ago"

    def test_never_negative(self):
        # Clock skew between the writer and this pane must not print "-3s ago".
        later, earlier = NOW_UTC.replace(second=50), NOW_UTC.replace(second=45)

        assert dashboard.format_age(later, earlier) == "0s ago"


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

    def test_stateless_panes_get_no_row(self):
        # They can never report state -- nothing hands them a `--status-script` -- so their
        # row was permanently dashes in every column. Three of the eight rows in the shipped
        # `full` profile were pure noise.
        sessions = [
            dashboard.RoleSession("coder", "claude", "Coder", "python"),
            dashboard.RoleSession("inbox", "claude", "Inbox", "inbox"),
            dashboard.RoleSession("dashboard", "claude", "Dashboard", "dashboard"),
            dashboard.RoleSession("cockpit", "claude", "Cockpit", "cockpit"),
        ]

        lines = dashboard.render_state_grid(sessions, {}, {}, NOW_UTC)

        assert any(line.startswith("coder") for line in lines)
        for hidden in ("inbox", "dashboard", "cockpit"):
            assert not any(line.startswith(hidden) for line in lines), hidden

    def test_a_partial_cost_total_still_counts_a_hidden_pane_s_backend(self):
        # `render_dashboard` hands the *unfiltered* list to `cost_is_partial`, which is why
        # the filtering lives in the grid rather than one level up.
        sessions = [
            dashboard.RoleSession("coder", "codex", "Coder", "python"),
            dashboard.RoleSession("cockpit", "claude", "Cockpit", "cockpit"),
        ]

        assert dashboard.cost_is_partial(sessions, {"coder": {"state": "idle"}}) is True

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


class TestStallDetection:
    """
    A role hung in `working` for an hour renders identically to one working normally, just
    with a larger SINCE -- and "larger" only means something against a number the dashboard
    did not have. The scheduler writes its own worker timeout into the status file.
    """

    def _working(self, *, seconds_ago, timeout=900, state="working"):
        since = NOW_UTC.timestamp() - seconds_ago
        return _status(
            state=state,
            since=datetime.fromtimestamp(since, UTC).isoformat().replace("+00:00", "Z"),
            worker_timeout_sec=timeout,
        )

    def test_working_past_the_timeout_is_a_stall(self):
        assert dashboard.is_stalled(self._working(seconds_ago=1000), NOW_UTC) is True

    def test_working_within_the_timeout_is_not(self):
        assert dashboard.is_stalled(self._working(seconds_ago=100), NOW_UTC) is False

    def test_a_retrying_role_can_stall_too(self):
        stuck = self._working(seconds_ago=1000, state="retrying")
        assert dashboard.is_stalled(stuck, NOW_UTC) is True

    def test_an_idle_role_is_never_stalled(self):
        # Idle for an hour is unemployed, not stuck. Flagging it would make the marker noise.
        idle = self._working(seconds_ago=100_000, state="idle")
        assert dashboard.is_stalled(idle, NOW_UTC) is False

    def test_no_recorded_timeout_is_not_a_stall(self):
        # Unknowable, not fine: a wrapper-mode role never writes one, and guessing a default
        # here would flag roles against a number nobody configured.
        status = _status(state="working", since="2020-01-01T00:00:00Z")
        assert dashboard.is_stalled(status, NOW_UTC) is False

    def test_no_status_file_is_not_a_stall(self):
        assert dashboard.is_stalled(None, NOW_UTC) is False

    def test_the_grid_marks_a_stalled_role(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": self._working(seconds_ago=1000)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        row = next(line for line in lines if line.startswith("coder"))
        assert dashboard.STALL_MARKER in row

    def test_the_grid_leaves_a_healthy_role_unmarked(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": self._working(seconds_ago=10)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        row = next(line for line in lines if line.startswith("coder"))
        assert dashboard.STALL_MARKER not in row

    def test_the_legend_appears_only_when_something_is_stalled(self):
        # A permanent legend for a usually-absent condition trains people to stop reading it.
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        healthy = _render(sessions, {"coder": self._working(seconds_ago=10)})
        stalled = _render(sessions, {"coder": self._working(seconds_ago=1000)})

        assert not any("worker may be hung" in line for line in healthy)
        assert any("worker may be hung" in line for line in stalled)


class TestAttemptCounter:
    def test_a_retry_shows_the_attempt(self):
        # `working` and `retrying` were distinct already, but with no N/max an
        # about-to-escalate role looked exactly like a healthy one.
        assert dashboard.attempt_suffix(_status(attempt=2, max_attempts=2)) == " 2/2"

    def test_a_first_attempt_shows_nothing(self):
        # Every cycle starts here; showing "1/2" on all of them would be pure noise.
        assert dashboard.attempt_suffix(_status(attempt=1, max_attempts=2)) == ""

    def test_a_role_that_never_reported_shows_nothing(self):
        assert dashboard.attempt_suffix(_status()) == ""
        assert dashboard.attempt_suffix(None) == ""

    def test_it_reaches_the_grid(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        statuses = {"coder": _status(state="retrying", attempt=2, max_attempts=2)}
        lines = dashboard.render_state_grid(sessions, statuses, {}, NOW_UTC)
        assert any("2/2" in line for line in lines)


class TestQueueWait:
    """
    Queue depth was already visible and is the weaker signal: one message unserved for an
    hour says something downstream is dead, five that arrived a minute ago say the swarm is
    busy. Depth alone cannot tell those apart.
    """

    def test_it_shows_the_age_of_the_oldest_queued_message(self):
        oldest = {"coder": "2026-08-09 16:30:00"}  # NOW_LOCAL is 17:00
        assert dashboard.queue_wait(oldest, "coder", NOW_LOCAL) == "30m"

    def test_an_empty_queue_shows_a_placeholder(self):
        assert dashboard.queue_wait({}, "coder", NOW_LOCAL) == "-"
        assert dashboard.queue_wait(None, "coder", NOW_LOCAL) == "-"

    def test_an_unparseable_timestamp_does_not_break_the_frame(self):
        assert dashboard.queue_wait({"coder": "not a date"}, "coder", NOW_LOCAL) == "-"

    def test_it_uses_the_local_parser_not_the_utc_one(self):
        # created_at is naive localtime by the schema's own default, while status `since` is
        # UTC. Reading it with the UTC parser would age every fresh message by the machine's
        # offset -- two hours, on this fixture's own clock.
        oldest = {"coder": NOW_LOCAL.strftime("%Y-%m-%d %H:%M:%S")}
        assert dashboard.queue_wait(oldest, "coder", NOW_LOCAL) == "0s"

    def test_it_reaches_the_grid(self):
        sessions = [dashboard.RoleSession("coder", "claude", "Coder")]
        lines = dashboard.render_state_grid(
            sessions, {}, {"coder": 1}, NOW_UTC,
            oldest_queued={"coder": "2026-08-09 16:30:00"}, now_local=NOW_LOCAL,
        )
        assert any("30m" in line for line in lines)


def _render(sessions, statuses, **overrides):
    kwargs = {
        "project_name": "demo",
        "branch": "main",
        "sessions": sessions,
        "statuses": statuses,
        "queue_depth": {},
        "messages": [],
        "now_utc": NOW_UTC,
        "now_local": NOW_LOCAL,
    }
    kwargs.update(overrides)
    return dashboard.render_dashboard(**kwargs)


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


class TestPromptWeight:
    """The proxy panel: what each role actually puts on the wire."""

    def _stats(self, **overrides):
        stats = {"coder": {"requests": 12, "avg_bytes": 104_200,
                           "max_bytes": 118_900, "total_bytes": 1_250_400,
                           "avg_tools": 33_300, "avg_system": 5_900,
                           "avg_messages": 80_200}}
        stats.update(overrides)
        return stats

    def test_shows_the_composition_split(self):
        # The split is the actionable part: system (the worker instructions) is ~5% of a
        # request while messages is 60-70%, which redirects where to optimise.
        row = next(line for line in dashboard.render_prompt_weight(self._stats())
                   if line.startswith("coder"))
        assert "33.3k" in row and "5.9k" in row and "80.2k" in row

    def test_shows_the_message_share(self):
        row = next(line for line in dashboard.render_prompt_weight(self._stats())
                   if line.startswith("coder"))
        assert "77%" in row  # 80200 / 104200

    def test_rows_without_composition_show_placeholders(self):
        # Captured before the columns existed, or an unparseable body.
        stats = {"coder": {"requests": 3, "avg_bytes": 1000, "max_bytes": 1000,
                           "total_bytes": 3000, "avg_tools": None,
                           "avg_system": None, "avg_messages": None}}
        row = next(line for line in dashboard.render_prompt_weight(stats)
                   if line.startswith("coder"))
        assert row.count("-") >= 3

    def test_shows_a_row_per_role(self):
        lines = dashboard.render_prompt_weight(self._stats())
        assert any(line.startswith("coder") for line in lines)

    def test_sizes_are_abbreviated(self):
        # Request sizes are read at a glance, not to the byte.
        row = next(line for line in dashboard.render_prompt_weight(self._stats())
                   if line.startswith("coder"))
        assert "104.2k" in row and "118.9k" in row

    def test_no_data_renders_no_panel(self):
        # The proxy is opt-in; an empty table would imply it ran and found nothing.
        assert dashboard.render_prompt_weight({}) == []

    def test_roles_are_ordered_predictably(self):
        stats = self._stats(architect={"requests": 1, "avg_bytes": 1, "max_bytes": 1,
                                       "total_bytes": 1})
        roles = [line.split()[0] for line in dashboard.render_prompt_weight(stats)
                 if line and line[0].isalpha() and not line.startswith(("ROLE", "Prompt"))]
        assert roles == sorted(roles)

    def test_the_panel_is_absent_from_a_dashboard_with_no_proxy(self):
        lines = dashboard.render_dashboard(
            project_name="p", branch="main",
            sessions=[dashboard.RoleSession("coder", "claude", "Coder")],
            statuses={}, queue_depth={}, messages=[],
            now_utc=NOW_UTC, now_local=NOW_LOCAL,
        )
        assert not any("Prompt weight" in line for line in lines)

    def test_the_panel_appears_when_there_is_traffic(self):
        lines = dashboard.render_dashboard(
            project_name="p", branch="main",
            sessions=[dashboard.RoleSession("coder", "claude", "Coder")],
            statuses={}, queue_depth={}, messages=[],
            now_utc=NOW_UTC, now_local=NOW_LOCAL,
            request_stats=self._stats(),
        )
        assert any("Prompt weight" in line for line in lines)


class TestPromptWeightScope:
    """
    The store outlives a run, so the panel must say which window it is showing.

    Averaging across runs blends configurations that are not comparable: one role measured
    at 220.8k was really 199k before a change and 118k after, and the mean describes
    neither.
    """

    def _stats(self):
        return {"coder": {"requests": 1, "avg_bytes": 1000, "max_bytes": 1000,
                          "total_bytes": 1000, "avg_tools": None,
                          "avg_system": None, "avg_messages": None}}

    def test_the_default_scope_is_stated(self):
        assert "this run" in dashboard.render_prompt_weight(self._stats())[1]

    def test_an_alternative_scope_is_stated(self):
        heading = dashboard.render_prompt_weight(self._stats(), scope="all history")[1]
        assert "all history" in heading

    def test_rows_older_than_the_window_are_excluded(self, tmp_path):
        from proxy.capture import TrafficRecord, TrafficStore

        store = TrafficStore(tmp_path / "traffic.db")
        store.ensure_schema()
        store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages",
                                   request_bytes=999_000, ts="2026-08-01T00:00:00Z"))
        store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages",
                                   request_bytes=1_000, ts="2026-08-13T00:00:00Z"))

        everything = store.request_stats_by_role()["coder"]
        this_run = store.request_stats_by_role(since="2026-08-12T00:00:00Z")["coder"]
        assert everything["requests"] == 2
        assert this_run["requests"] == 1
        assert this_run["avg_bytes"] == 1_000, "the older, much larger row must not skew it"

    def test_a_window_matching_nothing_hides_the_panel(self, tmp_path):
        from proxy.capture import TrafficRecord, TrafficStore

        store = TrafficStore(tmp_path / "traffic.db")
        store.ensure_schema()
        store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages",
                                   ts="2026-08-01T00:00:00Z"))
        assert store.request_stats_by_role(since="2026-08-13T00:00:00Z") == {}


class TestReadRequestStats:
    def test_no_path_is_no_data(self):
        assert dashboard.read_request_stats(None) == {}

    def test_a_missing_store_is_no_data(self, tmp_path):
        # The dashboard's job is the swarm; it must not die over an optional side channel.
        assert dashboard.read_request_stats(tmp_path / "absent.db") == {}

    def test_an_unreadable_store_is_no_data(self, tmp_path):
        junk = tmp_path / "traffic.db"
        junk.write_text("this is not a database", encoding="utf-8")
        assert dashboard.read_request_stats(junk) == {}


class TestFormatBytes:
    def test_small_counts_are_exact(self):
        assert dashboard._format_bytes(512) == "512"

    def test_thousands(self):
        assert dashboard._format_bytes(104_200) == "104.2k"

    def test_millions(self):
        assert dashboard._format_bytes(1_250_400) == "1.3M"


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
