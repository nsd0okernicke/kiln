"""
Tests for the read-only SQLite queue queries.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kiln.scheduler.infrastructure.persistence.queue_queries import (
    count_queued,
    count_queued_by_role,
    count_work_item_arrivals,
    cycles_by_work_item,
    oldest_queued_by_role,
    pending_for_role,
    recent_messages,
    work_item_messages,
)

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
def db() -> str:
    path = Path(tempfile.mktemp(suffix=".db"))
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return str(path)


def _insert(conn: sqlite3.Connection, **kw: object) -> None:
    fields = {
        "sender": "a",
        "target": "role",
        "status": "queued",
        "content": "test",
        "branch": "main",
        **kw,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO messages ({cols}) VALUES ({placeholders})",
        tuple(fields.values()),
    )


class TestCountQueued:
    def test_counts_queued_for_role(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, target="role", status="queued")
        _insert(conn, target="role", status="queued")
        _insert(conn, target="role", status="delivered")  # not counted
        conn.commit()
        conn.close()
        assert count_queued(db, "role", "main") == 2

    def test_returns_zero_when_empty(self, db: str) -> None:
        assert count_queued(db, "role", "main") == 0


class TestCountQueuedByRole:
    def test_groups_by_target(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, target="coder", status="queued")
        _insert(conn, target="coder", status="queued")
        _insert(conn, target="reviewer", status="queued")
        conn.commit()
        conn.close()
        result = count_queued_by_role(db, "main")
        assert result == {"coder": 2, "reviewer": 1}

    def test_skips_other_branch(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, target="coder", branch="other", status="queued")
        conn.commit()
        conn.close()
        assert count_queued_by_role(db, "main") == {}


class TestOldestQueuedByRole:
    def test_returns_oldest_timestamp(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, target="role", created_at="2025-01-01T00:00:00Z", status="queued")
        _insert(conn, target="role", created_at="2025-01-02T00:00:00Z", status="queued")
        conn.commit()
        conn.close()
        result = oldest_queued_by_role(db, "main")
        assert result["role"] == "2025-01-01T00:00:00Z"


class TestCyclesByWorkItem:
    def test_counts_per_work_item(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, work_item="CAT-1", created_at="2025-01-01T00:00:00Z")
        _insert(conn, work_item="CAT-1", created_at="2025-01-02T00:00:00Z")
        _insert(conn, work_item="CAT-2", created_at="2025-01-03T00:00:00Z")
        conn.commit()
        conn.close()
        result = cycles_by_work_item(db, "main")
        assert result == {"CAT-2": 1, "CAT-1": 2}  # newest first by min(created_at)

    def test_excludes_null_work_items(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, work_item=None, created_at="2025-01-01T00:00:00Z")
        conn.commit()
        conn.close()
        assert cycles_by_work_item(db, "main") == {}


class TestCountWorkItemArrivals:
    def test_counts_arrivals_to_target(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, work_item="CAT-1", target="coder")
        _insert(conn, work_item="CAT-1", target="coder")
        _insert(conn, work_item="CAT-1", target="reviewer")  # different target
        conn.commit()
        conn.close()
        assert count_work_item_arrivals(db, "CAT-1", "main", "coder") == 2


class TestRecentMessages:
    def test_returns_newest_first(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, content="old", created_at="2025-01-01T00:00:00Z")
        _insert(conn, content="new", created_at="2025-01-02T00:00:00Z")
        conn.commit()
        conn.close()
        msgs = recent_messages(db, "main", limit=10)
        assert [m["content"] for m in msgs] == ["new", "old"]

    def test_respects_limit(self, db: str) -> None:
        conn = sqlite3.connect(db)
        for i in range(5):
            _insert(conn, content=str(i))
        conn.commit()
        conn.close()
        assert len(recent_messages(db, "main", limit=3)) == 3


class TestWorkItemMessages:
    def test_includes_null_work_items(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, work_item=None, content="unnamed")
        _insert(conn, work_item="CAT-1", content="named")
        conn.commit()
        conn.close()
        msgs = work_item_messages(db, "main")
        assert len(msgs) == 2

    def test_orders_by_created_at_desc(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, content="old", created_at="2025-01-01T00:00:00Z")
        _insert(conn, content="new", created_at="2025-01-02T00:00:00Z")
        conn.commit()
        conn.close()
        msgs = work_item_messages(db, "main")
        assert [m["content"] for m in msgs] == ["new", "old"]


class TestPendingForRole:
    def test_returns_unacked_messages(self, db: str) -> None:
        conn = sqlite3.connect(db)
        _insert(conn, target="human", status="processed", acked_at=None)
        _insert(conn, target="human", status="processed", acked_at="2025-01-01T00:00:00Z")  # acked
        conn.commit()
        conn.close()
        msgs = pending_for_role(db, "main", "human")
        assert len(msgs) == 1
