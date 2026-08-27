"""
Tests for the backlog use cases (validation layer over task_store).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kiln.scheduler.application.backlog import (
    BacklogError,
    archive,
    create,
    handoff,
    list_all,
    show,
    update,
)
from kiln.scheduler.infrastructure.persistence.task_store import configure_context

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


class TestCreate:
    def test_validates_work_item_name(self, db: str) -> None:
        with pytest.raises(BacklogError, match="work-item name"):
            create(db, branch="main", work_item="", title="Title", body="Body")

    def test_validates_title(self, db: str) -> None:
        with pytest.raises(BacklogError, match="needs a title"):
            create(db, branch="main", work_item="CAT-1", title="", body="Body")

    def test_validates_body(self, db: str) -> None:
        with pytest.raises(BacklogError, match="needs a body"):
            create(db, branch="main", work_item="CAT-1", title="Title", body="")

    def test_creates_successfully(self, db: str) -> None:
        result = create(
            db, branch="main", work_item="CAT-1", title="Search", body="Add full-text search"
        )
        assert result["work_item"] == "CAT-1"
        assert result["title"] == "Search"

    def test_detects_duplicate(self, db: str) -> None:
        create(db, branch="main", work_item="CAT-1", title="A", body="B")
        with pytest.raises(BacklogError, match="already exists"):
            create(db, branch="main", work_item="CAT-1", title="C", body="D")


class TestList:
    def test_lists_all(self, db: str) -> None:
        create(db, branch="main", work_item="CAT-1", title="A", body="B")
        create(db, branch="main", work_item="CAT-2", title="C", body="D")
        assert len(list_all(db, branch="main")) == 2

    def test_rejects_unknown_status(self, db: str) -> None:
        with pytest.raises(BacklogError, match="unknown task status"):
            list_all(db, branch="main", status="invalid")


class TestShow:
    def test_returns_task(self, db: str) -> None:
        create(db, branch="main", work_item="CAT-1", title="A", body="B")
        task = show(db, branch="main", identifier="CAT-1")
        assert task["title"] == "A"

    def test_raises_when_not_found(self, db: str) -> None:
        with pytest.raises(BacklogError, match="was not found"):
            show(db, branch="main", identifier="NONEXISTENT")


class TestUpdate:
    def test_updates(self, db: str) -> None:
        task = create(db, branch="main", work_item="CAT-1", title="Old", body="Old")
        updated = update(db, branch="main", identifier=task["id"], title="New")
        assert updated["title"] == "New"

    def test_rejects_empty_title(self, db: str) -> None:
        task = create(db, branch="main", work_item="CAT-1", title="Old", body="Old")
        with pytest.raises(BacklogError, match="needs a title"):
            update(db, branch="main", identifier=task["id"], title="")

    def test_rejects_empty_body(self, db: str) -> None:
        task = create(db, branch="main", work_item="CAT-1", title="Old", body="Old")
        with pytest.raises(BacklogError, match="needs a body"):
            update(db, branch="main", identifier=task["id"], body="")


class TestArchive:
    def test_archives(self, db: str) -> None:
        task = create(db, branch="main", work_item="CAT-1", title="A", body="B")
        archived = archive(db, branch="main", identifier=task["id"])
        assert archived["status"] == "archived"

    def test_raises_if_already_archived(self, db: str) -> None:
        task = create(db, branch="main", work_item="CAT-1", title="A", body="B")
        archive(db, branch="main", identifier=task["id"])
        with pytest.raises(BacklogError):
            archive(db, branch="main", identifier=task["id"])


class TestHandoff:
    def test_handoff_requires_context(self, db: str) -> None:
        configure_context(db, branch="main", human_role="human", intake_role="specifier")
        task = create(db, branch="main", work_item="CAT-1", title="Search", body="Add search")
        result = handoff(db, branch="main", identifier=task["id"], sender="human", target="coder")
        assert result["status"] == "active"
        assert result["message_id"] is not None

    def test_raises_if_already_handed_off(self, db: str) -> None:
        configure_context(db, branch="main", human_role="human", intake_role="specifier")
        task = create(db, branch="main", work_item="CAT-1", title="A", body="B")
        handoff(db, branch="main", identifier=task["id"], sender="human", target="coder")
        with pytest.raises(BacklogError):
            handoff(db, branch="main", identifier=task["id"], sender="human", target="coder")
