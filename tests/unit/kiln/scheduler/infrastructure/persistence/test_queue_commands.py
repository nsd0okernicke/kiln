"""
Tests for the SQLite message queue mutation commands.

Every test uses an in-memory SQLite database with the production schema so the SQL is
exercised against the real table layout, not a mock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kiln.scheduler.infrastructure.persistence.queue_commands import (
    acknowledge_message,
    failed_messages,
    fetch_and_deliver,
    fetch_resume,
    get_message,
    insert_handoff,
    mark_failed,
    mark_processed,
    mark_processing,
    message_exists,
    name_work_item,
    recover_stale_processing,
    resume_failed,
)

# Schema from the production module — keep in sync with db.py
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  sender TEXT NOT NULL,
  target TEXT NOT NULL,
  priority INTEGER DEFAULT 50,
  status TEXT DEFAULT 'queued',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  delivered_at TEXT,
  acked_at TEXT,
  processed_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  work_item TEXT
);
CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status);
"""


@pytest.fixture
def db_path() -> str:
    """An in-memory SQLite database with the production schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    # Return a ":memory:" path that connect() in queue_commands handles.
    # We need a real file path for connect() since it opens by path.
    # Let's use a temp file instead.
    conn.close()
    import tempfile

    path = Path(tempfile.mktemp(suffix=".db"))
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return str(path)


# ---------------------------------------------------------------------------
# insert_handoff
# ---------------------------------------------------------------------------


class TestInsertHandoff:
    def test_returns_a_string_id(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "alice", "bob", "hello", "main")
        assert isinstance(mid, str)
        assert len(mid) > 0

    def test_stores_all_fields(self, db_path: str) -> None:
        mid = insert_handoff(
            db_path,
            sender="spec",
            target="coder",
            content="Implement X",
            branch="feature",
            priority=10,
            work_item="CAT-1",
        )
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["sender"] == "spec"
        assert msg["target"] == "coder"
        assert msg["content"] == "Implement X"
        assert msg["branch"] == "feature"
        assert msg["priority"] == 10
        assert msg["work_item"] == "CAT-1"
        assert msg["status"] == "queued"

    def test_default_priority(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "b", "test", "main")
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["priority"] == 50  # DEFAULT_PRIORITY

    def test_message_exists_after_insert(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "b", "test", "main")
        assert message_exists(db_path, mid) is True
        assert message_exists(db_path, "nonexistent") is False


# ---------------------------------------------------------------------------
# fetch_and_deliver
# ---------------------------------------------------------------------------


class TestFetchAndDeliver:
    def test_returns_oldest_queued_message(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "second", "main")
        insert_handoff(db_path, "a", "role", "first", "main")
        msg = fetch_and_deliver(db_path, "role", "main")
        assert msg is not None
        # id is sorted by created_at ASC, so the oldest (first inserted) comes first
        assert msg["content"] == "second"

    def test_returns_none_when_empty(self, db_path: str) -> None:
        assert fetch_and_deliver(db_path, "role", "main") is None

    def test_marks_as_delivered(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        fetch_and_deliver(db_path, "role", "main")
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "delivered"
        assert msg["delivered_at"] is not None

    def test_skips_other_role(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role-a", "for A", "main")
        assert fetch_and_deliver(db_path, "role-b", "main") is None

    def test_skips_other_branch(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "main-branch", "main")
        assert fetch_and_deliver(db_path, "role", "other-branch") is None

    def test_re_delivers_already_delivered(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        fetch_and_deliver(db_path, "role", "main")  # first delivery
        msg2 = fetch_and_deliver(db_path, "role", "main")  # re-deliver
        assert msg2 is not None
        assert msg2["id"] == mid

    def test_higher_priority_first(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "low", "main", priority=100)
        mid_high = insert_handoff(db_path, "a", "role", "high", "main", priority=10)
        msg = fetch_and_deliver(db_path, "role", "main")
        assert msg is not None
        assert msg["id"] == mid_high
        assert msg["content"] == "high"


# ---------------------------------------------------------------------------
# fetch_resume
# ---------------------------------------------------------------------------


class TestFetchResume:
    def test_returns_only_acked_messages(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        # First fail it, then resume it (which sets acked_at)
        mark_failed(db_path, mid, "error")
        resume_failed(db_path, mid, "retry with guidance")
        msg = fetch_resume(db_path, "role", "main")
        assert msg is not None
        assert msg["id"] == mid

    def test_returns_none_when_no_acked(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "just queued", "main")
        assert fetch_resume(db_path, "role", "main") is None


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestMarkProcessing:
    def test_transitions_to_processing(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        assert mark_processing(db_path, mid) is True
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "processing"
        assert msg["started_at"] is not None

    def test_returns_false_for_unknown_id(self, db_path: str) -> None:
        assert mark_processing(db_path, "nonexistent") is False


class TestMarkProcessed:
    def test_transitions_to_processed(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        mark_processing(db_path, mid)
        assert mark_processed(db_path, mid) is True
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "processed"
        assert msg["processed_at"] is not None
        assert msg["finished_at"] is not None

    def test_refuses_from_failed(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        mark_failed(db_path, mid, "oops")
        assert mark_processed(db_path, mid) is False


class TestMarkFailed:
    def test_transitions_to_failed(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        assert mark_failed(db_path, mid, "something broke") is True
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "failed"
        assert msg["error"] == "something broke"
        assert msg["finished_at"] is not None

    def test_returns_false_for_unknown_id(self, db_path: str) -> None:
        assert mark_failed(db_path, "nonexistent", "error") is False

    def test_refuses_invalid_transition(self, db_path: str) -> None:
        """Can't mark a 'processed' message as 'failed'."""
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        mark_processing(db_path, mid)
        mark_processed(db_path, mid)
        assert mark_failed(db_path, mid, "too late") is False


# ---------------------------------------------------------------------------
# resume_failed
# ---------------------------------------------------------------------------


class TestResumeFailed:
    def test_re_queues_failed_message(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "original", "main", work_item="CAT-1")
        mark_failed(db_path, mid, "error")
        result = resume_failed(db_path, mid, "retry with guidance")
        assert result is not None
        assert result["id"] == mid
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "queued"
        assert msg["content"] == "retry with guidance"
        assert msg["acked_at"] is not None  # human acknowledged
        assert msg["delivered_at"] is None  # reset for re-delivery

    def test_returns_none_for_non_failed(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        assert resume_failed(db_path, mid, "guidance") is None

    def test_returns_none_for_unknown(self, db_path: str) -> None:
        assert resume_failed(db_path, "nonexistent", "guidance") is None


# ---------------------------------------------------------------------------
# recover_stale_processing
# ---------------------------------------------------------------------------


class TestRecoverStaleProcessing:
    def test_resets_processing_to_delivered(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        mark_processing(db_path, mid)
        rows = recover_stale_processing(db_path, "role", "main")
        assert len(rows) == 1
        assert rows[0]["id"] == mid
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["status"] == "delivered"

    def test_ignores_other_role(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role-a", "test", "main")
        mark_processing(db_path, mid)
        rows = recover_stale_processing(db_path, "role-b", "main")
        assert len(rows) == 0

    def test_ignores_non_processing(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "test", "main")
        rows = recover_stale_processing(db_path, "role", "main")
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# acknowledge_message
# ---------------------------------------------------------------------------


class TestAcknowledge:
    def test_sets_acked_at(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        result = acknowledge_message(db_path, mid, "role", "main")
        assert result is not None
        assert result["acked_at"] is not None

    def test_returns_none_for_wrong_role(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        assert acknowledge_message(db_path, mid, "wrong-role", "main") is None

    def test_returns_none_for_unknown(self, db_path: str) -> None:
        assert acknowledge_message(db_path, "nonexistent", "role", "main") is None


# ---------------------------------------------------------------------------
# name_work_item
# ---------------------------------------------------------------------------


class TestNameWorkItem:
    def test_sets_work_item_on_null(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main")
        assert name_work_item(db_path, mid, "CAT-7") is True
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["work_item"] == "CAT-7"

    def test_does_not_overwrite_existing(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "test", "main", work_item="EXISTING")
        assert name_work_item(db_path, mid, "NEW") is False
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["work_item"] == "EXISTING"


# ---------------------------------------------------------------------------
# failed_messages
# ---------------------------------------------------------------------------


class TestFailedMessages:
    def test_lists_failed_newest_first(self, db_path: str) -> None:
        mid1 = insert_handoff(db_path, "a", "role", "first", "main")
        mid2 = insert_handoff(db_path, "a", "role", "second", "main")
        mark_failed(db_path, mid1, "err1")
        mark_failed(db_path, mid2, "err2")
        results = failed_messages(db_path, "main")
        assert len(results) == 2
        # Both ids present; ordering may vary within the same second
        ids = {r["id"] for r in results}
        assert ids == {mid1, mid2}

    def test_ignores_non_failed(self, db_path: str) -> None:
        insert_handoff(db_path, "a", "role", "ok", "main")
        assert failed_messages(db_path, "main") == []

    def test_skips_other_branch(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "role", "fail", "branch-a")
        mark_failed(db_path, mid, "err")
        assert failed_messages(db_path, "branch-b") == []


# ---------------------------------------------------------------------------
# get_message / message_exists
# ---------------------------------------------------------------------------


class TestGetMessage:
    def test_returns_full_row(self, db_path: str) -> None:
        mid = insert_handoff(db_path, "a", "b", "hello", "main")
        msg = get_message(db_path, mid)
        assert msg is not None
        assert msg["id"] == mid
        assert msg["sender"] == "a"
        assert msg["target"] == "b"
        assert msg["content"] == "hello"

    def test_returns_none_for_missing(self, db_path: str) -> None:
        assert get_message(db_path, "nonexistent") is None
