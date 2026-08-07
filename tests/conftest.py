"""Shared fixtures. Tests run against a real scratch SQLite file, never a mock."""

from __future__ import annotations

import uuid
from contextlib import closing

import pytest
from scheduler import db


@pytest.fixture
def db_path(tmp_path):
    """Path to an initialised, empty messages.db."""
    path = tmp_path / ".kiln" / "messages.db"
    db.ensure_schema(path)
    return path


@pytest.fixture
def add_message(db_path):
    """
    Insert a message with fully explicit column values.

    Deliberately raw SQL rather than db.insert_handoff: these tests exercise the readers,
    so they need precise control over created_at, status and id instead of inheriting
    whatever the writer happens to generate.
    """

    def _add(
        *,
        sender="specifier",
        target="coder",
        content="body",
        branch="main",
        priority=db.DEFAULT_PRIORITY,
        status=db.STATUS_QUEUED,
        created_at="2026-01-01 00:00:00",
        message_id=None,
    ):
        message_id = message_id or uuid.uuid4().hex
        with closing(db.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, sender, target, priority, status, content, created_at, branch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, sender, target, priority, status, content, created_at, branch),
            )
            conn.commit()
        return message_id

    return _add


@pytest.fixture
def read_message(db_path):
    """Fetch one message row back as a dict, for asserting on writer side effects."""

    def _read(message_id):
        with closing(db.connect(db_path)) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return dict(row) if row else None

    return _read
