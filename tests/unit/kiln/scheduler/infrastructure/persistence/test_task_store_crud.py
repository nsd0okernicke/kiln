"""
Tests for the task store persistence layer and backlog use cases.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kiln.scheduler.infrastructure.persistence.task_store import (
    TaskConflictError,
    archive_task,
    configure_context,
    create_task,
    get_context,
    get_task,
    handoff_task,
    list_tasks,
    update_task,
)

# Schema from db.py — messages + tasks + task_context
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
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  branch TEXT NOT NULL,
  work_item TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog' CHECK (status IN ('backlog', 'active', 'archived')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  dispatched_at TEXT,
  message_id TEXT REFERENCES messages(id),
  UNIQUE (branch, work_item)
);
CREATE TABLE IF NOT EXISTS task_context (
  branch TEXT PRIMARY KEY,
  human_role TEXT NOT NULL,
  intake_role TEXT NOT NULL
);
"""


@pytest.fixture
def db() -> str:
    path = Path(tempfile.mktemp(suffix=".db"))
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return str(path)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


class TestCreateAndGet:
    def test_creates_and_returns_task(self, db: str) -> None:
        task = create_task(
            db,
            branch="main",
            work_item="CAT-1",
            title="Add search",
            body="Add full-text search to the catalog",
        )
        assert task["work_item"] == "CAT-1"
        assert task["title"] == "Add search"
        assert task["status"] == "backlog"

        fetched = get_task(db, branch="main", identifier="CAT-1")
        assert fetched is not None
        assert fetched["id"] == task["id"]

    def test_can_fetch_by_id(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-2", title="Fix", body="Fix bug")
        fetched = get_task(db, branch="main", identifier=task["id"])
        assert fetched is not None
        assert fetched["work_item"] == "CAT-2"

    def test_duplicate_work_item_raises(self, db: str) -> None:
        create_task(db, branch="main", work_item="CAT-1", title="First", body="Body")
        with pytest.raises(TaskConflictError, match="already exists"):
            create_task(db, branch="main", work_item="CAT-1", title="Second", body="Body")

    def test_same_work_item_different_branch(self, db: str) -> None:
        create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        create_task(db, branch="other", work_item="CAT-1", title="C", body="D")
        assert len(list_tasks(db, branch="main")) == 1
        assert len(list_tasks(db, branch="other")) == 1


class TestList:
    def test_lists_all(self, db: str) -> None:
        create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        create_task(db, branch="main", work_item="CAT-2", title="C", body="D")
        tasks = list_tasks(db, branch="main")
        assert len(tasks) == 2

    def test_filters_by_status(self, db: str) -> None:
        t1 = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        create_task(db, branch="main", work_item="CAT-2", title="C", body="D")
        archive_task(db, branch="main", identifier=t1["id"])
        tasks = list_tasks(db, branch="main", status="backlog")
        assert len(tasks) == 1

    def test_skips_other_branch(self, db: str) -> None:
        create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        assert list_tasks(db, branch="other") == []


class TestUpdate:
    def test_updates_title_and_body(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="Old", body="Old body")
        updated = update_task(
            db, branch="main", identifier=task["id"], title="New title", body="New body"
        )
        assert updated["title"] == "New title"
        assert updated["body"] == "New body"
        assert updated["updated_at"] is not None

    def test_raises_if_archived(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        archive_task(db, branch="main", identifier=task["id"])
        with pytest.raises(TaskConflictError):
            update_task(db, branch="main", identifier=task["id"], title="New", body="New")


class TestArchive:
    def test_archives_task(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        archived = archive_task(db, branch="main", identifier=task["id"])
        assert archived["status"] == "archived"

    def test_raises_if_already_archived(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        archive_task(db, branch="main", identifier=task["id"])
        with pytest.raises(TaskConflictError):
            archive_task(db, branch="main", identifier=task["id"])

    def test_get_by_archived_work_item(self, db: str) -> None:
        """Archived tasks are still retrievable by identifier."""
        task = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        archive_task(db, branch="main", identifier=task["id"])
        fetched = get_task(db, branch="main", identifier="CAT-1")
        assert fetched is not None
        assert fetched["status"] == "archived"


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------


class TestHandoff:
    def test_queues_message_and_activates_task(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="Search", body="Add search")
        result = handoff_task(
            db, branch="main", identifier=task["id"], sender="human", target="coder"
        )
        assert result["status"] == "active"
        assert result["message_id"] is not None

    def test_raises_if_active(self, db: str) -> None:
        task = create_task(db, branch="main", work_item="CAT-1", title="A", body="B")
        handoff_task(db, branch="main", identifier=task["id"], sender="human", target="coder")
        with pytest.raises(TaskConflictError):
            handoff_task(db, branch="main", identifier=task["id"], sender="human", target="coder")

    def test_raises_if_unknown(self, db: str) -> None:
        with pytest.raises(TaskConflictError):
            handoff_task(
                db, branch="main", identifier="NONEXISTENT", sender="human", target="coder"
            )


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class TestContext:
    def test_configure_and_get(self, db: str) -> None:
        configure_context(db, branch="main", human_role="human", intake_role="specifier")
        ctx = get_context(db, branch="main")
        assert ctx is not None
        assert ctx["human_role"] == "human"
        assert ctx["intake_role"] == "specifier"

    def test_returns_none_when_not_configured(self, db: str) -> None:
        assert get_context(db, branch="main") is None

    def test_update_existing(self, db: str) -> None:
        configure_context(db, branch="main", human_role="human", intake_role="specifier")
        configure_context(db, branch="main", human_role="operator", intake_role="specifier")
        ctx = get_context(db, branch="main")
        assert ctx is not None
        assert ctx["human_role"] == "operator"
