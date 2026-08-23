"""
The cockpit's pure builders — the JSON half of issue #22.

Everything here is data in, data out. The snapshot is built by hand rather than read from a
running swarm, exactly as `test_dashboard.py` builds its fixtures, because the questions being
asked ("which lane does this card sit in", "is this role hot") are decided by rules, not by
whether SQLite is available.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kiln.cockpit.application import state as cockpit_state
from kiln.scheduler.domain import handoff
from kiln.scheduler.infrastructure.cli.dashboard import RoleSession, SwarmSnapshot
from kiln.scheduler.infrastructure.persistence import db

pytestmark = pytest.mark.integration

NOW_UTC = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)
NOW_LOCAL = datetime(2026, 8, 9, 17, 0, 0)


def _snapshot(*, sessions=None, statuses=None, queue_depth=None, oldest=None, messages=None):
    return SwarmSnapshot(
        sessions=sessions if sessions is not None else [
            RoleSession("specifier", "claude", "Specifier"),
            RoleSession("coder", "claude", "Coder"),
        ],
        statuses=statuses or {},
        queue_depth=queue_depth or {},
        oldest_queued=oldest or {},
        messages=messages or [],
        request_stats={},
        now_utc=NOW_UTC,
        now_local=NOW_LOCAL,
    )


def _row(
    *,
    work_item="ORDER-INTAKE",
    sender="specifier",
    target="coder",
    status=db.STATUS_QUEUED,
    created_at="2026-08-09 16:59:00",
    message_id="a" * 32,
    summary="did the thing",
    escalation=False,
    error=None,
):
    return {
        "id": message_id,
        "sender": sender,
        "target": target,
        "status": status,
        "work_item": work_item,
        "created_at": created_at,
        "error": error,
        "content": handoff.format_handoff(
            sender=sender, handoff=work_item, branch="main", commit="abc1234",
            summary=summary, next_role=target, timestamp=created_at, escalation=escalation,
        ),
    }


class TestLaneFor:
    """
    The one rule that decides where a card is drawn, and the only place role names could
    have crept into the board. They must not: the shipped `full` profile's shape is one of
    several, and a lane rule that knows about `architect` is wrong in every other profile.
    """

    def test_a_queued_message_puts_the_card_in_its_targets_lane(self):
        assert cockpit_state.lane_for(_row(target="coder")) == "coder"

    def test_a_delivered_message_still_belongs_to_the_target(self):
        row = _row(target="refactorer", status=db.STATUS_DELIVERED)

        assert cockpit_state.lane_for(row) == "refactorer"

    def test_a_processed_message_means_nothing_is_holding_the_item(self):
        # Any handoff the target sent onward would itself be the newest message and would be
        # the row examined instead, so "consumed and nothing followed" is genuinely done.
        row = _row(status=db.STATUS_PROCESSED)

        assert cockpit_state.lane_for(row) == cockpit_state.LANE_DONE

    def test_a_failed_message_stays_with_the_role_that_failed_on_it(self):
        # Not a lane of its own: retry sends the same row back to the same role, so moving
        # the card somewhere else would misdescribe where the work actually is.
        row = _row(target="coder", status=db.STATUS_FAILED, error="tests failed")

        assert cockpit_state.lane_for(row) == "coder"


class TestBuildBoard:
    def test_only_the_latest_message_per_work_item_becomes_a_card(self):
        rows = [
            _row(work_item="ALPHA", target="refactorer", created_at="2026-08-09 16:59:00"),
            _row(work_item="ALPHA", target="coder", created_at="2026-08-09 16:50:00"),
        ]

        board = cockpit_state.build_board(rows, {"ALPHA": 2}, NOW_LOCAL, ("coder", "refactorer"))

        assert [card["work_item"] for card in board["cards"]["refactorer"]] == ["ALPHA"]
        assert board["cards"]["coder"] == []

    def test_lanes_keep_profile_order_and_done_comes_last(self):
        board = cockpit_state.build_board([], {}, NOW_LOCAL, ("specifier", "coder"))

        assert board["lanes"] == ["specifier", "coder", "done"]

    def test_a_card_for_a_role_that_is_not_a_lane_is_still_shown(self):
        # A role dropped from the profile between runs leaves real work behind. Losing the
        # card would make the board quietly disagree with the queue it is drawn from.
        rows = [_row(work_item="ORPHAN", target="reviewer")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("coder",))

        assert board["cards"]["reviewer"][0]["work_item"] == "ORPHAN"

    def test_lanes_are_inferred_from_traffic_when_none_are_configured(self):
        rows = [_row(work_item="ALPHA", target="coder")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ())

        assert board["lanes"] == ["coder", "done"]

    def test_an_unnamed_request_is_a_card_in_its_targets_lane(self):
        # The bug this fixes: a human's intake hop has no work item -- the specifier is what
        # invents one -- so the board drew nothing at all for the minutes between "I asked
        # for something" and "the specifier finished naming it". Measured at 8 minutes on a
        # live run, during which the operator could not tell the request from a lost one.
        rows = [_row(work_item=None, sender="human-in-the-loop", target="specifier")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier", "coder"))

        assert len(board["cards"]["specifier"]) == 1
        assert board["cards"]["specifier"][0]["unnamed"] is True

    def test_an_unnamed_card_borrows_the_requests_opening_line_as_its_title(self):
        rows = [_row(work_item=None, target="specifier", summary="handoff the next userstory")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier",))

        assert board["cards"]["specifier"][0]["title"] == "handoff the next userstory"
        assert board["cards"]["specifier"][0]["work_item"] is None

    def test_two_unnamed_requests_are_two_cards(self):
        # They share the absence of a name, which is not a thing to group by: two people
        # asking for two unrelated features are two pieces of work.
        rows = [
            _row(work_item=None, target="specifier", message_id="a" * 32, summary="first"),
            _row(work_item=None, target="specifier", message_id="b" * 32, summary="second"),
        ]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier",))

        assert [card["title"] for card in board["cards"]["specifier"]] == ["first", "second"]

    def test_a_consumed_unnamed_request_leaves_no_card_behind(self):
        # Once the specifier consumes it and hands on under a real name, the named card is
        # the work. Keeping the placeholder would strand a duplicate in Done describing
        # something that is still moving.
        rows = [
            _row(work_item="ALPHA", target="coder", created_at="2026-08-09 16:59:00"),
            _row(
                work_item=None, target="specifier", status=db.STATUS_PROCESSED,
                message_id="b" * 32, created_at="2026-08-09 16:50:00",
            ),
        ]

        board = cockpit_state.build_board(rows, {"ALPHA": 2}, NOW_LOCAL, ("specifier", "coder"))

        assert board["cards"]["specifier"] == []
        assert board["cards"]["done"] == []
        assert [card["work_item"] for card in board["cards"]["coder"]] == ["ALPHA"]

    def test_a_request_that_failed_before_being_named_stays_visible(self):
        # The opposite case, and the one that must not be swept up with it: a request that
        # stopped before it ever had a name is exactly what a human has to see.
        rows = [_row(work_item=None, target="specifier", status=db.STATUS_FAILED)]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier",))

        assert board["cards"]["specifier"][0]["failed"] is True

    def test_a_literal_pending_work_item_counts_as_unnamed(self):
        # Not hypothetical: `kiln-handoff` has wrapper-mode agents write this column by hand
        # in raw SQL, so one that copies the placeholder through lands `pending` here. Taken
        # as a real name it becomes a card every unrelated request piles into.
        rows = [_row(work_item="pending", target="specifier", summary="do the thing")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier",))

        card = board["cards"]["specifier"][0]
        assert card["unnamed"] is True
        assert card["title"] == "do the thing"

    def test_an_unnamed_card_counts_itself_rather_than_reporting_zero(self):
        # `cycles_by_work_item` is keyed by work item, so an unnamed row has no entry and the
        # lookup returns 0 -- a card on screen claiming no messages exist.
        rows = [_row(work_item=None, target="specifier")]

        board = cockpit_state.build_board(rows, {}, NOW_LOCAL, ("specifier",))

        assert board["cards"]["specifier"][0]["cycles"] == 1

    def test_a_card_carries_its_lap_count(self):
        rows = [_row(work_item="ALPHA")]

        board = cockpit_state.build_board(rows, {"ALPHA": 7}, NOW_LOCAL, ("coder",))

        assert board["cards"]["coder"][0]["cycles"] == 7


class TestActivityHeat:
    def test_a_running_worker_is_hot(self):
        assert cockpit_state.activity_heat({"state": "working"}, 0) == 1.0

    def test_a_retrying_worker_is_just_as_hot(self):
        assert cockpit_state.activity_heat({"state": "retrying"}, 0) == 1.0

    def test_an_idle_role_with_a_queue_is_warm(self):
        # The interesting case: work has arrived and nothing is moving it yet.
        assert cockpit_state.activity_heat({"state": "idle"}, 3) == 0.5

    def test_an_idle_role_with_an_empty_queue_is_cold(self):
        assert cockpit_state.activity_heat({"state": "idle"}, 0) == 0.0

    def test_a_role_with_no_status_file_is_cold_rather_than_an_error(self):
        assert cockpit_state.activity_heat(None, 0) == 0.0


class TestRoleRowsHidesStatelessPanes:
    """
    `inbox`, `dashboard` and `cockpit` never write a status file, so every column of their
    Work Queue row was a dash. Filtered through the same `visible_roles` the terminal grid
    uses, so the two views cannot disagree about which roles exist.
    """

    def _sessions(self):
        return [
            RoleSession("human-in-the-loop", "claude", "Human", "agent"),
            RoleSession("inbox", "claude", "Inbox", "inbox"),
            RoleSession("specifier", "claude", "Specifier", "python"),
            RoleSession("dashboard", "claude", "Dashboard", "dashboard"),
            RoleSession("cockpit", "claude", "Cockpit", "cockpit"),
        ]

    def test_only_roles_that_can_report_state_get_a_row(self):
        rows = cockpit_state.role_rows(_snapshot(sessions=self._sessions()), [])

        assert [row["role"] for row in rows] == ["human-in-the-loop", "specifier"]

    def test_a_hidden_panes_queue_depth_does_not_leak_into_another_row(self):
        snapshot = _snapshot(sessions=self._sessions(), queue_depth={"inbox": 4})

        rows = cockpit_state.role_rows(snapshot, [])

        assert all(row["queue"] == 0 for row in rows)

    def test_a_legacy_sessions_file_still_lists_everything(self):
        # Four-column rows default to `agent`, so an upgrade cannot silently empty the table.
        legacy = [RoleSession("specifier", "claude", "Specifier")]

        rows = cockpit_state.role_rows(_snapshot(sessions=legacy), [])

        assert [row["role"] for row in rows] == ["specifier"]


class TestRoleRows:
    def test_rows_follow_the_sessions_order(self):
        rows = cockpit_state.role_rows(_snapshot(), [])

        assert [row["role"] for row in rows] == ["specifier", "coder"]

    def test_it_reports_which_backend_and_model_a_role_runs(self):
        # The agent comes from the sessions file, the model from the role's own status file
        # -- the model the worker was actually invoked with, not what the profile asked for.
        statuses = {"coder": {"state": "idle", "model": "claude-sonnet-5"}}

        rows = cockpit_state.role_rows(_snapshot(statuses=statuses), [])

        assert rows[1]["agent"] == "claude"
        assert rows[1]["model"] == "claude-sonnet-5"

    def test_a_wrapper_role_falls_back_to_the_profiles_model(self):
        # `human-in-the-loop` has no scheduler, so nothing ever writes it a status model.
        # The sessions file is its only source — without it the column stayed empty forever.
        sessions = [RoleSession("human-in-the-loop", "claude", "Human", "agent",
                                "claude-sonnet-5")]

        rows = cockpit_state.role_rows(_snapshot(sessions=sessions), [])

        assert rows[0]["model"] == "claude-sonnet-5"

    def test_the_resolved_model_beats_the_profiles(self):
        # A scheduler role's status carries what it actually resolved, including a model that
        # came from the worker definition's frontmatter and appears in no profile.
        sessions = [RoleSession("coder", "claude", "Coder", "python", "claude-sonnet-5")]
        statuses = {"coder": {"state": "idle", "model": "claude-opus-5"}}

        rows = cockpit_state.role_rows(_snapshot(sessions=sessions, statuses=statuses), [])

        assert rows[0]["model"] == "claude-opus-5"

    def test_a_role_that_has_not_reported_yet_has_no_model(self):
        # None, not a guess from the profile: until the scheduler writes a status there is
        # genuinely no answer, and a frontmatter model would make any guess wrong.
        rows = cockpit_state.role_rows(_snapshot(), [])

        assert rows[1]["model"] is None

    def test_a_role_with_no_status_reports_nothing_rather_than_zero(self):
        # Same rule the dashboard's `-` columns follow: a role that never tracked cost must
        # not appear to have measured $0.00.
        row = cockpit_state.role_rows(_snapshot(), [])[0]

        assert row["state"] is None
        assert row["cost_usd"] is None
        assert row["cycles"] is None

    def test_a_status_file_supplies_state_and_spend(self):
        statuses = {"coder": {
            "state": "working", "since": "2026-08-09T14:59:30Z",
            "cycles": 2, "cost_usd": 1.5, "tokens": 1000,
            "token_usage": {"input": 250, "cache_read": 750},
        }}

        row = cockpit_state.role_rows(_snapshot(statuses=statuses), [])[1]

        assert row["state"] == "working"
        assert row["since_ago"] == "30s ago"
        assert row["cost_usd"] == 1.5
        assert row["cache_share"] == 0.75

    def test_a_role_past_its_worker_timeout_is_flagged_stalled(self):
        statuses = {"coder": {
            "state": "working", "since": "2026-08-09T14:00:00Z", "worker_timeout_sec": 900,
        }}

        row = cockpit_state.role_rows(_snapshot(statuses=statuses), [])[1]

        assert row["stalled"] is True

    def test_the_work_item_a_role_holds_comes_from_the_queue(self):
        # There is nowhere else it could come from: `set-status.py` writes state, cost and
        # tokens, and has never had a work-item field.
        rows = cockpit_state.role_rows(_snapshot(), [_row(work_item="ALPHA", target="coder")])

        assert rows[1]["work_item"] == "ALPHA"

    def test_a_finished_message_does_not_make_a_role_look_busy(self):
        work_items = [_row(work_item="ALPHA", target="coder", status=db.STATUS_PROCESSED)]

        rows = cockpit_state.role_rows(_snapshot(), work_items)

        assert rows[1]["work_item"] is None

    def test_an_unnamed_request_shows_its_opening_line_rather_than_nothing(self):
        work_items = [
            _row(work_item=None, target="specifier", summary="handoff the next userstory")
        ]

        rows = cockpit_state.role_rows(_snapshot(), work_items)

        assert rows[0]["work_item"] == "handoff the next userstory"

    def test_a_named_message_wins_over_an_older_unnamed_one_for_the_same_role(self):
        # The `setdefault` trap: unnamed rows reach this loop now, and `setdefault` keys on
        # presence, so the first row seen would claim the role and refuse to be replaced --
        # leaving the column showing a superseded request forever.
        work_items = [
            _row(
                work_item="ALPHA", target="specifier", message_id="a" * 32,
                created_at="2026-08-09 16:59:00",
            ),
            _row(
                work_item=None, target="specifier", message_id="b" * 32,
                summary="older request", created_at="2026-08-09 16:50:00",
            ),
        ]

        rows = cockpit_state.role_rows(_snapshot(), work_items)

        assert rows[0]["work_item"] == "ALPHA"

    def test_queue_wait_reads_created_at_as_local_time(self):
        # The bug this guards: `created_at` is naive localtime and `since` is UTC. Parsing
        # the first with the UTC parser reports every fresh message as hours old on any
        # machine that is not on UTC — including the one this project is developed on.
        snapshot = _snapshot(oldest={"coder": "2026-08-09 16:55:00"})

        rows = cockpit_state.role_rows(snapshot, [])

        assert rows[1]["wait"] == "5m ago"


class TestBuildAttention:
    def test_failed_cycles_rank_above_completed_ones(self):
        failed = [{
            "id": "f" * 32, "sender": "specifier", "target": "coder",
            "work_item": "ALPHA", "error": "tests failed",
            "created_at": "2026-08-09 16:00:00",
        }]
        awaiting = [_row(work_item="BETA", target="human-in-the-loop")]

        items = cockpit_state.build_attention(
            failed=failed, awaiting_human=awaiting, messages=[], now_local=NOW_LOCAL,
            human_role="human-in-the-loop",
        )

        assert [item["kind"] for item in items] == ["failed", "review"]

    def test_a_failed_row_is_retryable_and_a_review_is_not(self):
        failed = [{
            "id": "f" * 32, "sender": "specifier", "target": "coder", "work_item": "ALPHA",
            "error": "tests failed", "created_at": "2026-08-09 16:00:00",
        }]

        items = cockpit_state.build_attention(
            failed=failed, awaiting_human=[_row(target="human-in-the-loop")], messages=[],
            now_local=NOW_LOCAL, human_role="human-in-the-loop",
        )

        assert [item["retryable"] for item in items] == [True, False]

    def test_an_escalation_already_covered_by_a_failed_row_is_not_listed_twice(self):
        # `_escalate` writes both a `failed` row and an escalation message for the same work
        # item. Counting them separately would double the swarm's only real alarm.
        failed = [{
            "id": "f" * 32, "sender": "coder", "target": "coder", "work_item": "ALPHA",
            "error": "gave up", "created_at": "2026-08-09 16:00:00",
        }]
        escalation = _row(work_item="ALPHA", target="human-in-the-loop", escalation=True)

        items = cockpit_state.build_attention(
            failed=failed, awaiting_human=[], messages=[escalation], now_local=NOW_LOCAL,
            human_role="human-in-the-loop",
        )

        assert [item["kind"] for item in items] == ["failed"]

    def test_an_uncovered_escalation_is_still_surfaced(self):
        escalation = _row(work_item="BETA", target="human-in-the-loop", escalation=True)

        items = cockpit_state.build_attention(
            failed=[], awaiting_human=[], messages=[escalation], now_local=NOW_LOCAL,
            human_role="human-in-the-loop",
        )

        assert [item["kind"] for item in items] == ["escalation"]

    def test_an_empty_swarm_needs_no_attention(self):
        items = cockpit_state.build_attention(
            failed=[], awaiting_human=[], messages=[], now_local=NOW_LOCAL,
            human_role="human-in-the-loop",
        )

        assert items == []


class TestBuildTotals:
    def test_totals_sum_every_role_that_reported(self):
        statuses = {
            "specifier": {"state": "idle", "cost_usd": 0.5, "cycles": 1, "tokens": 100},
            "coder": {"state": "idle", "cost_usd": 1.5, "cycles": 2, "tokens": 200},
        }

        totals = cockpit_state.build_totals(_snapshot(statuses=statuses))

        assert (totals["cost_usd"], totals["cycles"], totals["tokens"]) == (2.0, 3, 300)

    def test_a_backend_that_reports_no_cost_marks_the_total_partial(self):
        # Structurally incomplete, not merely small — the distinction the dashboard's `+`
        # marker exists to make, carried into the payload rather than re-derived in the page.
        snapshot = _snapshot(
            sessions=[RoleSession("coder", "codex", "Coder")],
            statuses={"coder": {"state": "idle", "tokens": 100}},
        )

        assert cockpit_state.build_totals(snapshot)["cost_partial"] is True


class TestBuildState:
    def test_the_payload_carries_every_section_the_page_renders(self):
        ctx = cockpit_state.CockpitContext(
            project_name="demo", branch="main", lanes=("specifier", "coder"),
            intake_role="specifier",
        )

        payload = cockpit_state.build_state(
            ctx, _snapshot(messages=[_row()]), work_items=[_row()], cycles={"ORDER-INTAKE": 1},
            failed=[], awaiting_human=[], activity_limit=5,
        )

        assert set(payload) == {
            "project", "branch", "human_role", "intake_role", "generated_at", "roles",
            "totals", "board", "attention", "activity", "request_stats", "work_items",
        }
        assert payload["project"] == "demo"
        assert payload["intake_role"] == "specifier"

    def test_it_offers_the_known_work_items_newest_first(self):
        # What the composer's picker lists. Retyping a name is how "cat3" quietly becomes a
        # second bucket for CAT-3.
        ctx = cockpit_state.CockpitContext(project_name="demo", branch="main")

        payload = cockpit_state.build_state(
            ctx, _snapshot(), work_items=[], cycles={"CAT-3": 4, "cat-1-search-books": 6},
            failed=[], awaiting_human=[], activity_limit=5,
        )

        assert payload["work_items"] == ["CAT-3", "cat-1-search-books"]

    def test_a_fresh_project_offers_none(self):
        ctx = cockpit_state.CockpitContext(project_name="demo", branch="main")

        payload = cockpit_state.build_state(
            ctx, _snapshot(), work_items=[], cycles={}, failed=[], awaiting_human=[],
            activity_limit=5,
        )

        assert payload["work_items"] == []

    def test_the_activity_feed_honours_its_limit(self):
        messages = [_row(message_id=str(index) * 32) for index in range(5)]
        ctx = cockpit_state.CockpitContext(project_name="demo", branch="main")

        payload = cockpit_state.build_state(
            ctx, _snapshot(messages=messages), work_items=[], cycles={}, failed=[],
            awaiting_human=[], activity_limit=2,
        )

        assert len(payload["activity"]) == 2
