"""
Queue semantics against a real SQLite file. Ordering, scoping and status transitions are
what keep two agents from picking up the same handoff or stranding one forever.
"""

from __future__ import annotations

from contextlib import closing

import pytest
from scheduler import db


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
        assert db.verify_queued(db_path, "specifier", "main") == message_id

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "deep" / ".kiln" / "messages.db"
        db.ensure_schema(nested)
        assert nested.is_file()

    def test_defaults_are_applied_when_columns_are_omitted(self, db_path):
        # The schema's own defaults matter: agents historically omitted created_at.
        with closing(db.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO messages (sender, target, content) VALUES ('a', 'b', 'c')"
            )
            conn.commit()
            row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["id"]
        assert row["created_at"]
        assert row["status"] == db.STATUS_QUEUED
        assert row["priority"] == db.DEFAULT_PRIORITY
        assert row["branch"] == "main"


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

    def test_only_this_branch(self, db_path, add_message):
        add_message(target="coder", branch="main")
        add_message(target="coder", branch="other")
        rows = db.recent_messages(db_path, "main")
        assert len(rows) == 1

    def test_empty_db_is_an_empty_list(self, db_path):
        assert db.recent_messages(db_path, "main") == []


class TestStatusTransitions:
    def test_mark_processing(self, db_path, add_message, read_message):
        message_id = add_message()
        assert db.mark_processing(db_path, message_id) is True
        assert read_message(message_id)["status"] == db.STATUS_PROCESSING

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

    @pytest.mark.parametrize("operation", [db.mark_processing, db.mark_processed])
    def test_unknown_id_reports_failure(self, db_path, operation):
        assert operation(db_path, "does-not-exist") is False

    def test_full_lifecycle(self, db_path, add_message, read_message):
        message_id = add_message(target="coder")
        db.fetch_and_deliver(db_path, "coder", "main")
        db.mark_processing(db_path, message_id)
        db.mark_processed(db_path, message_id)
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_PROCESSED
        assert stored["delivered_at"] and stored["processed_at"]


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


class TestVerifyQueued:
    def test_finds_the_most_recent_queued_message(self, db_path, add_message):
        add_message(sender="coder", created_at="2026-01-01 00:00:00", message_id="older")
        add_message(sender="coder", created_at="2026-01-02 00:00:00", message_id="newer")
        assert db.verify_queued(db_path, "coder", "main") == "newer"

    def test_returns_none_when_the_insert_never_landed(self, db_path):
        assert db.verify_queued(db_path, "coder", "main") is None

    def test_scoped_to_sender(self, db_path, add_message):
        add_message(sender="refactorer")
        assert db.verify_queued(db_path, "coder", "main") is None

    def test_scoped_to_branch(self, db_path, add_message):
        add_message(sender="coder", branch="other")
        assert db.verify_queued(db_path, "coder", "main") is None

    def test_ignores_messages_that_left_the_queue(self, db_path, add_message):
        add_message(sender="coder", status=db.STATUS_DELIVERED)
        assert db.verify_queued(db_path, "coder", "main") is None

    def test_confirms_a_freshly_inserted_handoff(self, db_path):
        # The insert/verify pair from kiln-handoff/SKILL.md steps 4-5.
        message_id = db.insert_handoff(db_path, "coder", "refactorer", "payload", "main")
        assert db.verify_queued(db_path, "coder", "main") == message_id
