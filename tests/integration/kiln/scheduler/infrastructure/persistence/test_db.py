"""
Queue semantics against a real SQLite file. Ordering, scoping and status transitions are
what keep two agents from picking up the same handoff or stranding one forever.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from kiln.scheduler.application.ports import QueueAccessError
from kiln.scheduler.infrastructure.persistence import db, queue_commands
from kiln.scheduler.infrastructure.persistence.sqlite_message_queue import SQLiteMessageQueue


class TestSchema:
    def test_creates_table_and_index(self, db_path):
        query = "SELECT name FROM sqlite_master WHERE type=?"
        with closing(db.connect(db_path)) as conn:
            tables = {r[0] for r in conn.execute(query, ("table",))}
            indexes = {r[0] for r in conn.execute(query, ("index",))}
        assert "messages" in tables
        assert "idx_target_branch_status" in indexes

    def test_is_idempotent(self, db_path, add_message):
        message_id = add_message()
        db.ensure_schema(db_path)  # re-running init must not wipe the queue
        assert db.message_exists(db_path, message_id) is True

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "deep" / ".kiln" / "messages.db"
        db.ensure_schema(nested)
        assert nested.is_file()

    def test_defaults_are_applied_when_columns_are_omitted(self, db_path):
        # The schema's own defaults matter: agents historically omitted created_at.
        with closing(db.connect(db_path)) as conn:
            conn.execute("INSERT INTO messages (sender, target, content) VALUES ('a', 'b', 'c')")
            conn.commit()
            row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["id"]
        assert row["created_at"].endswith("Z")
        assert row["status"] == db.STATUS_QUEUED
        assert row["priority"] == db.DEFAULT_PRIORITY
        assert row["branch"] == "main"

    def test_adds_run_timing_columns_to_an_existing_database(self, tmp_path):
        path = tmp_path / "legacy.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                "CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, target TEXT, "
                "status TEXT, content TEXT, created_at TEXT, branch TEXT, work_item TEXT)"
            )
            conn.commit()

        db.ensure_schema(path)

        with closing(db.connect(path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert {"started_at", "finished_at"} <= columns


class TestFetchAndDeliver:
    def test_empty_inbox_returns_none(self, db_path):
        assert db.fetch_and_deliver(db_path, "coder", "main") is None

    def test_returns_message_and_marks_it_delivered(self, db_path, add_message, read_message):
        message_id = add_message(target="coder", content="do the thing")

        message = db.fetch_and_deliver(db_path, "coder", "main")

        assert message["id"] == message_id
        assert message["content"] == "do the thing"
        assert message["sender"] == "specifier"
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_DELIVERED
        assert stored["delivered_at"]

    def test_redelivers_an_already_delivered_message(self, db_path, add_message):
        # An agent that died between delivery and processing must get the work back
        # rather than have it stranded.
        message_id = add_message(target="coder", status=db.STATUS_DELIVERED)
        assert db.fetch_and_deliver(db_path, "coder", "main")["id"] == message_id

    @pytest.mark.parametrize("status", [db.STATUS_PROCESSING, db.STATUS_PROCESSED])
    def test_ignores_messages_already_past_delivery(self, db_path, add_message, status):
        add_message(target="coder", status=status)
        assert db.fetch_and_deliver(db_path, "coder", "main") is None

    def test_scoped_to_target_role(self, db_path, add_message):
        add_message(target="refactorer")
        assert db.fetch_and_deliver(db_path, "coder", "main") is None

    def test_scoped_to_branch(self, db_path, add_message):
        add_message(target="coder", branch="feature-x")
        assert db.fetch_and_deliver(db_path, "coder", "main") is None
        assert db.fetch_and_deliver(db_path, "coder", "feature-x") is not None

    def test_lower_priority_number_wins(self, db_path, add_message):
        add_message(target="coder", priority=50, content="normal")
        add_message(target="coder", priority=5, content="urgent")
        assert db.fetch_and_deliver(db_path, "coder", "main")["content"] == "urgent"

    def test_ties_broken_by_creation_time(self, db_path, add_message):
        add_message(target="coder", priority=50, created_at="2026-01-02 00:00:00", content="newer")
        add_message(target="coder", priority=50, created_at="2026-01-01 00:00:00", content="older")
        assert db.fetch_and_deliver(db_path, "coder", "main")["content"] == "older"

    def test_priority_outranks_creation_time(self, db_path, add_message):
        add_message(target="coder", priority=50, created_at="2026-01-01 00:00:00", content="older")
        add_message(target="coder", priority=1, created_at="2026-01-02 00:00:00", content="urgent")
        assert db.fetch_and_deliver(db_path, "coder", "main")["content"] == "urgent"

    def test_delivers_one_message_at_a_time(self, db_path, add_message):
        add_message(target="coder", created_at="2026-01-01 00:00:00", content="first")
        add_message(target="coder", created_at="2026-01-02 00:00:00", content="second")
        assert db.fetch_and_deliver(db_path, "coder", "main")["content"] == "first"
        # Still returned second because delivered messages are re-delivered until processed.
        db.mark_processed(db_path, db.fetch_and_deliver(db_path, "coder", "main")["id"])
        assert db.fetch_and_deliver(db_path, "coder", "main")["content"] == "second"


class TestCountQueued:
    def test_counts_only_queued_for_this_role_and_branch(self, db_path, add_message):
        add_message(target="coder", branch="main")
        add_message(target="coder", branch="main")
        add_message(target="coder", branch="other")
        add_message(target="refactorer", branch="main")
        add_message(target="coder", branch="main", status=db.STATUS_DELIVERED)
        assert db.count_queued(db_path, "coder", "main") == 2

    def test_empty_queue_counts_zero(self, db_path):
        assert db.count_queued(db_path, "coder", "main") == 0


class TestCountQueuedByRole:
    def test_groups_by_target_for_this_branch(self, db_path, add_message):
        add_message(target="coder", branch="main")
        add_message(target="coder", branch="main")
        add_message(target="refactorer", branch="main")
        add_message(target="coder", branch="other")
        add_message(target="coder", branch="main", status=db.STATUS_DELIVERED)
        assert db.count_queued_by_role(db_path, "main") == {"coder": 2, "refactorer": 1}

    def test_empty_queue_is_an_empty_dict(self, db_path):
        assert db.count_queued_by_role(db_path, "main") == {}


class TestRecentMessages:
    def test_newest_first(self, db_path, add_message):
        add_message(target="coder", created_at="2026-01-01 00:00:00")
        add_message(target="refactorer", created_at="2026-01-02 00:00:00")
        rows = db.recent_messages(db_path, "main")
        assert [r["target"] for r in rows] == ["refactorer", "coder"]

    def test_respects_the_limit(self, db_path, add_message):
        for _ in range(5):
            add_message(target="coder")
        assert len(db.recent_messages(db_path, "main", limit=3)) == 3

    def test_default_limit_is_ten(self, db_path, add_message):
        for _ in range(12):
            add_message(target="coder")
        assert len(db.recent_messages(db_path, "main")) == 10

    def test_only_this_branch(self, db_path, add_message):
        add_message(target="coder", branch="main")
        add_message(target="coder", branch="other")
        rows = db.recent_messages(db_path, "main")
        assert len(rows) == 1

    def test_empty_db_is_an_empty_list(self, db_path):
        assert db.recent_messages(db_path, "main") == []


class TestWorkItemMessages:
    def test_default_window_returns_the_latest_120_messages(self, db_path, add_message):
        for index in range(121):
            add_message(target="coder", content=str(index))

        messages = db.work_item_messages(db_path, "main")

        assert len(messages) == 120
        assert messages[0]["content"] == "120"
        assert messages[-1]["content"] == "1"


class TestStatusTransitions:
    def test_names_the_initial_inbound_after_the_specifier_names_work(
        self, db_path, add_message, read_message
    ):
        message_id = add_message(work_item=None)

        assert db.name_work_item(db_path, message_id, "CAT-3") is True
        assert read_message(message_id)["work_item"] == "CAT-3"

    def test_does_not_replace_an_existing_work_item_name(
        self, db_path, add_message, read_message
    ):
        message_id = add_message(work_item="CAT-2")

        assert db.name_work_item(db_path, message_id, "CAT-3") is False
        assert read_message(message_id)["work_item"] == "CAT-2"

    def test_mark_processing(self, db_path, add_message, read_message):
        message_id = add_message()
        assert db.mark_processing(db_path, message_id) is True
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_PROCESSING
        assert stored["started_at"].endswith("Z")

    def test_mark_processing_leaves_processed_at_unset(self, db_path, add_message, read_message):
        message_id = add_message()
        db.mark_processing(db_path, message_id)
        assert read_message(message_id)["processed_at"] is None

    def test_mark_processed_stamps_the_time(self, db_path, add_message, read_message):
        message_id = add_message()
        assert db.mark_processed(db_path, message_id) is True
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_PROCESSED
        assert stored["processed_at"]
        assert stored["finished_at"].endswith("Z")

    @pytest.mark.parametrize("operation", [db.mark_processing, db.mark_processed])
    def test_unknown_id_reports_failure(self, db_path, operation):
        assert operation(db_path, "does-not-exist") is False

    @pytest.mark.parametrize("operation", [db.mark_processing, db.mark_processed])
    def test_unknown_stored_status_reports_failure(self, db_path, add_message, operation):
        message_id = add_message(status="not-a-status")
        assert operation(db_path, message_id) is False

    def test_invalid_transition_reports_failure(self, db_path, add_message):
        message_id = add_message(status=db.STATUS_PROCESSED)
        assert db.mark_processing(db_path, message_id) is False

    def test_full_lifecycle(self, db_path, add_message, read_message):
        message_id = add_message(target="coder")
        db.fetch_and_deliver(db_path, "coder", "main")
        db.mark_processing(db_path, message_id)
        db.mark_processed(db_path, message_id)
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_PROCESSED
        assert stored["delivered_at"] and stored["processed_at"]


class TestRecoverStaleProcessing:
    """
    A message marked `processing` when its scheduler is killed is stranded: `fetch_and_deliver`
    re-serves `queued` and `delivered` but not `processing`, and every count filters on
    `queued`, so it is neither re-served nor visible anywhere. The work is silently lost, and
    stopping a swarm mid-cycle is the normal way to cause it.
    """

    def test_a_stranded_message_is_served_again(self, db_path, add_message):
        message_id = add_message(target="coder", status=db.STATUS_PROCESSING)

        db.recover_stale_processing(db_path, "coder", "main")

        assert db.fetch_and_deliver(db_path, "coder", "main")["id"] == message_id

    def test_it_reports_what_it_recovered(self, db_path, add_message):
        # Returned rather than counted so the scheduler can log each one: an operator who
        # sees a handoff processed twice needs to know which message was replayed.
        add_message(
            target="coder", status=db.STATUS_PROCESSING, sender="specifier", work_item="add-login"
        )

        recovered = db.recover_stale_processing(db_path, "coder", "main")

        assert [(r["sender"], r["work_item"]) for r in recovered] == [("specifier", "add-login")]

    def test_it_leaves_a_live_role_alone(self, db_path, add_message, read_message):
        # The scoping argument is the whole safety case: exactly one process serves a role's
        # queue, so *this* role's processing rows are stale by definition. Another role's
        # are not -- that scheduler is still running and still working the message.
        mine = add_message(target="coder", status=db.STATUS_PROCESSING)
        theirs = add_message(target="refactorer", status=db.STATUS_PROCESSING)

        db.recover_stale_processing(db_path, "coder", "main")

        assert read_message(mine)["status"] == db.STATUS_DELIVERED
        assert read_message(theirs)["status"] == db.STATUS_PROCESSING

    def test_it_is_scoped_to_the_branch(self, db_path, add_message, read_message):
        other = add_message(target="coder", branch="feature-x", status=db.STATUS_PROCESSING)
        db.recover_stale_processing(db_path, "coder", "main")
        assert read_message(other)["status"] == db.STATUS_PROCESSING

    @pytest.mark.parametrize("status", [db.STATUS_QUEUED, db.STATUS_DELIVERED, db.STATUS_PROCESSED])
    def test_it_touches_nothing_else(self, db_path, add_message, read_message, status):
        # Resurrecting a *processed* message would re-run finished work every restart.
        message_id = add_message(target="coder", status=status)
        assert db.recover_stale_processing(db_path, "coder", "main") == []
        assert read_message(message_id)["status"] == status

    def test_an_empty_queue_recovers_nothing(self, db_path):
        assert db.recover_stale_processing(db_path, "coder", "main") == []

    def test_recovery_is_idempotent(self, db_path, add_message):
        # Restarting twice in a row must not report the same message again -- the second
        # run finds it `delivered`, which is a normal state, not a stranded one.
        add_message(target="coder", status=db.STATUS_PROCESSING)
        assert len(db.recover_stale_processing(db_path, "coder", "main")) == 1
        assert db.recover_stale_processing(db_path, "coder", "main") == []


class TestSQLiteMessageQueue:
    def test_recovery_translates_sqlite_errors(self, db_path, monkeypatch):
        def fail_recovery(*_args):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(queue_commands, "recover_stale_processing", fail_recovery)

        with pytest.raises(QueueAccessError, match="database is locked"):
            SQLiteMessageQueue(db_path).recover_processing("coder", "main")


class TestCountWorkItemArrivals:
    """
    The unit behind the max-cycles guard: how many times one work item has reached one role.
    That is the number of laps, independent of how many roles a profile happens to have.
    """

    def test_counts_arrivals_for_one_role(self, db_path, add_message):
        for _ in range(3):
            add_message(target="coder", work_item="add-login")
        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 3

    def test_a_different_work_item_is_a_different_count(self, db_path, add_message):
        add_message(target="coder", work_item="add-login")
        add_message(target="coder", work_item="fix-search")
        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 1

    def test_a_different_role_is_a_different_count(self, db_path, add_message):
        # Counting every message for the item would fold lap *length* into the number, so one
        # ceiling would mean different things in a 4-role profile and a 2-role one.
        add_message(target="refactorer", work_item="add-login")
        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 0

    def test_processed_laps_still_count(self, db_path, add_message):
        # Only counting live messages would let a swarm loop forever without tripping.
        add_message(target="coder", work_item="add-login", status=db.STATUS_PROCESSED)
        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 1

    def test_it_is_scoped_to_the_branch(self, db_path, add_message):
        add_message(target="coder", work_item="add-login", branch="feature-x")
        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 0


class TestFailedAndResume:
    """
    Escalation used to be a dead end: the inbound was marked `processed`, so a failed cycle
    was indistinguishable from a successful one and there was nothing left to address. The
    `error` and `acked_at` columns were declared with the table and never written by any code.
    """

    def test_failing_a_message_records_the_reason(self, db_path, add_message, read_message):
        message_id = add_message(target="coder")

        assert db.mark_failed(db_path, message_id, "worker blocked: missing fixtures") is True

        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_FAILED
        assert stored["error"] == "worker blocked: missing fixtures"
        assert stored["finished_at"].endswith("Z")

    def test_failing_an_unknown_message_reports_failure(self, db_path):
        assert db.mark_failed(db_path, "does-not-exist", "nope") is False

    def test_failing_a_message_with_an_unknown_status_reports_failure(self, db_path, add_message):
        message_id = add_message(status="not-a-status")
        assert db.mark_failed(db_path, message_id, "nope") is False

    def test_failing_a_processed_message_reports_failure(self, db_path, add_message):
        message_id = add_message(status=db.STATUS_PROCESSED)
        assert db.mark_failed(db_path, message_id, "nope") is False

    def test_failing_leaves_processed_at_unset(self, db_path, add_message, read_message):
        # It did not complete. Stamping processed_at would make it look like it did.
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "nope")
        assert read_message(message_id)["processed_at"] is None

    def test_a_failed_message_is_never_re_served(self, db_path, add_message):
        # The invariant the `failed` status buys: it comes back only when a human sends it.
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "nope")
        assert db.fetch_and_deliver(db_path, "coder", "main") is None

    def test_resuming_re_queues_the_same_row(self, db_path, add_message, read_message):
        # A new row would look like brand-new work to every guard that counts per work item.
        message_id = add_message(target="coder", work_item="add-login")
        db.mark_failed(db_path, message_id, "nope")

        db.resume_failed(db_path, message_id, "new content")

        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_QUEUED
        assert stored["content"] == "new content"
        assert stored["work_item"] == "add-login", "the work item identity must survive"
        assert stored["started_at"] is None
        assert stored["finished_at"] is None

    def test_resuming_stamps_the_acknowledgement(self, db_path, add_message, read_message):
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "nope")
        db.resume_failed(db_path, message_id, "c")
        assert read_message(message_id)["acked_at"]

    def test_resuming_keeps_the_failure_reason(self, db_path, add_message, read_message):
        # Still answerable afterwards: what went wrong the first time.
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "missing fixtures")
        db.resume_failed(db_path, message_id, "c")
        assert read_message(message_id)["error"] == "missing fixtures"

    @pytest.mark.parametrize(
        "status", [db.STATUS_QUEUED, db.STATUS_DELIVERED, db.STATUS_PROCESSING]
    )
    def test_only_a_failed_message_can_be_resumed(self, db_path, add_message, status):
        # Re-queueing something merely `processing` would hand a live scheduler a second
        # copy of the work it is already doing.
        message_id = add_message(target="coder", status=status)
        assert db.resume_failed(db_path, message_id, "c") is None

    def test_resuming_an_unknown_id_reports_failure(self, db_path):
        assert db.resume_failed(db_path, "nope", "c") is None

    def test_failed_messages_are_listable_with_their_reasons(self, db_path, add_message):
        first = add_message(target="coder", work_item="add-login")
        db.mark_failed(db_path, first, "missing fixtures")
        add_message(target="coder")  # still queued; must not appear

        listed = db.failed_messages(db_path, "main")

        assert [(r["id"], r["error"]) for r in listed] == [(first, "missing fixtures")]


class TestFetchResume:
    """
    What a halted role polls. `acked_at` is written only by `resume_failed`, so it separates
    "a human looked at this and sent it back" from every other queued message -- without
    inventing a second status meaning the same thing.
    """

    def test_it_ignores_ordinary_queued_work(self, db_path, add_message):
        add_message(target="coder")
        assert db.fetch_resume(db_path, "coder", "main") is None

    def test_it_returns_a_resumed_message(self, db_path, add_message):
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "nope")
        db.resume_failed(db_path, message_id, "content")

        assert db.fetch_resume(db_path, "coder", "main")["id"] == message_id

    def test_it_marks_the_resumed_message_delivered(self, db_path, add_message, read_message):
        message_id = add_message(target="coder")
        db.mark_failed(db_path, message_id, "nope")
        db.resume_failed(db_path, message_id, "content")
        db.fetch_resume(db_path, "coder", "main")
        assert read_message(message_id)["status"] == db.STATUS_DELIVERED

    def test_it_is_scoped_to_the_role(self, db_path, add_message):
        message_id = add_message(target="refactorer")
        db.mark_failed(db_path, message_id, "nope")
        db.resume_failed(db_path, message_id, "content")
        assert db.fetch_resume(db_path, "coder", "main") is None


class TestInsertHandoff:
    def test_returns_a_usable_id(self, db_path, read_message):
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        assert read_message(message_id)["content"] == "payload"

    def test_queues_with_expected_defaults(self, db_path, read_message):
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_QUEUED
        assert stored["priority"] == db.DEFAULT_PRIORITY
        assert stored["sender"] == "coder"
        assert stored["target"] == "refactorer"
        assert stored["branch"] == "main"
        assert stored["created_at"]

    def test_priority_is_overridable(self, db_path, read_message):
        message_id = db.insert_handoff(db_path, "architect", "specifier", "x", "main", priority=1)
        assert read_message(message_id)["priority"] == 1

    def test_ids_are_distinct(self, db_path):
        first = db.insert_handoff(db_path, "coder", "refactorer", "a", "main")
        second = db.insert_handoff(db_path, "coder", "refactorer", "b", "main")
        assert first != second

    def test_inserted_handoff_is_immediately_deliverable(self, db_path):
        db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        assert db.fetch_and_deliver(db_path, "refactorer", "main")["content"] == "payload"

    def test_multiline_content_survives_round_trip(self, db_path, read_message):
        content = "Sender: coder\nHandoff: order-intake\nBranch: main\nCommit: abc123\n\n✓ DONE"
        message_id = db.insert_handoff(db_path, "coder", "refactorer", content, "main")
        assert read_message(message_id)["content"] == content


class TestMessageLookup:
    def test_returns_an_existing_message(self, db_path, add_message):
        message_id = add_message(content="payload")
        assert db.get_message(db_path, message_id)["content"] == "payload"

    def test_returns_none_for_an_unknown_message(self, db_path):
        assert db.get_message(db_path, "does-not-exist") is None


class TestAcknowledgeMessage:
    def test_returns_none_when_the_message_is_out_of_scope(self, db_path, add_message):
        message_id = add_message(target="coder")
        assert db.acknowledge_message(db_path, message_id, "refactorer", "main") is None


class TestMessageExists:
    """
    How an insert is confirmed, and why it is by id.

    Found live: `verify_queued` asked "is there a *queued* message from me?", the receiving
    scheduler took the message one second after it was written, and the check reported the
    insert had failed. The sender obediently sent the whole handoff again -- the specifier ran
    two full cycles on one request and the coder was handed two competing specs.
    """

    def test_confirms_a_freshly_inserted_handoff(self, db_path):
        # The insert/verify pair from kiln-handoff/SKILL.md steps 4-5.
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        assert db.message_exists(db_path, message_id) is True

    def test_an_insert_that_never_landed_is_not_confirmed(self, db_path):
        assert db.message_exists(db_path, "never-inserted") is False

    @pytest.mark.parametrize(
        "status",
        [db.STATUS_DELIVERED, db.STATUS_PROCESSING, db.STATUS_PROCESSED, db.STATUS_FAILED],
    )
    def test_a_consumer_cannot_race_the_confirmation_away(self, db_path, status):
        # The whole point. Every one of these states used to read as "your insert failed".
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        db._set_status(db_path, message_id, status)
        assert db.message_exists(db_path, message_id) is True

    def test_another_senders_message_does_not_confirm_mine(self, db_path, add_message):
        # An id is unique, so this cannot happen by accident -- but the check it replaced
        # would happily confirm a *different* message that merely shared sender and branch.
        add_message(sender="coder", message_id="someone-elses")
        assert db.message_exists(db_path, "mine") is False


class TestWorkItem:
    """
    The grouping key the queue never had.

    `branch` holds the *base* branch, shared by every role on a swarm, so it groups
    everything into one bucket. Without a real key nothing can answer "what did this
    feature cost" or "how many cycles has it been round", and loop detection has nothing
    to count.
    """

    def test_the_column_and_its_index_exist(self, db_path):
        with closing(db.connect(db_path)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            indexes = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
        assert "work_item" in columns
        assert "idx_work_item" in indexes

    def test_it_is_stored_when_given(self, db_path, read_message):
        message_id = db.insert_handoff(
            db_path, "coder", "refactorer", "payload", "main", work_item="CAT-3 search"
        )
        assert read_message(message_id)["work_item"] == "CAT-3 search"

    def test_it_is_null_when_absent(self, db_path, read_message):
        # The intake hop legitimately has none: the specifier is what invents the name.
        message_id = db.insert_handoff(db_path, "human-in-the-loop", "specifier", "x", "main")
        assert read_message(message_id)["work_item"] is None

    def test_an_empty_name_is_stored_as_null_not_empty_string(self, db_path, read_message):
        # Two spellings of "no work item" would split the same group in a GROUP BY.
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "x", "main", work_item="")
        assert read_message(message_id)["work_item"] is None


class TestOldestQueuedByRole:
    def test_returns_the_oldest_queued_message_per_role(self, db_path, add_message):
        add_message(target="coder", created_at="2026-01-02 00:00:00")
        add_message(target="coder", created_at="2026-01-01 00:00:00")
        add_message(target="refactorer", created_at="2026-01-03 00:00:00")

        assert db.oldest_queued_by_role(db_path, "main") == {
            "coder": "2026-01-01 00:00:00",
            "refactorer": "2026-01-03 00:00:00",
        }

    def test_only_queued_messages_count(self, db_path, add_message):
        # A delivered message is being worked; its age is the role's SINCE, not a queue wait.
        add_message(target="coder", status=db.STATUS_DELIVERED, created_at="2026-01-01 00:00:00")
        add_message(target="coder", created_at="2026-01-05 00:00:00")
        assert db.oldest_queued_by_role(db_path, "main") == {"coder": "2026-01-05 00:00:00"}

    def test_an_empty_queue_yields_nothing(self, db_path):
        assert db.oldest_queued_by_role(db_path, "main") == {}

    def test_it_is_scoped_to_the_branch(self, db_path, add_message):
        add_message(target="coder", branch="feature-x")
        assert db.oldest_queued_by_role(db_path, "main") == {}


class TestCyclesByWorkItem:
    def test_counts_messages_per_work_item(self, db_path):
        for _ in range(3):
            db.insert_handoff(db_path, "coder", "refactorer", "x", "main", work_item="CAT-3")
        db.insert_handoff(db_path, "coder", "refactorer", "x", "main", work_item="CAT-4")
        assert db.cycles_by_work_item(db_path, "main") == {"CAT-3": 3, "CAT-4": 1}

    def test_intake_messages_are_excluded(self, db_path):
        # Not a cycle, and grouping it under a NULL key would invite counting it as one.
        db.insert_handoff(db_path, "human-in-the-loop", "specifier", "x", "main")
        db.insert_handoff(db_path, "specifier", "coder", "x", "main", work_item="CAT-3")
        assert db.cycles_by_work_item(db_path, "main") == {"CAT-3": 1}

    def test_other_branches_are_not_counted(self, db_path):
        db.insert_handoff(db_path, "coder", "refactorer", "x", "main", work_item="CAT-3")
        db.insert_handoff(db_path, "coder", "refactorer", "x", "other", work_item="CAT-3")
        assert db.cycles_by_work_item(db_path, "main") == {"CAT-3": 1}

    def test_an_empty_queue_is_empty_not_an_error(self, db_path):
        assert db.cycles_by_work_item(db_path, "main") == {}
