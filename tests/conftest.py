"""Shared fixtures. Tests run against a real scratch SQLite file, never a mock."""

from __future__ import annotations

import subprocess
import uuid
from contextlib import closing

import pytest
from scheduler import db


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """
    Give every test a git identity through the environment.

    Several tests make real commits, which git refuses outright without `user.name`/
    `user.email`. The suite used to inherit whatever the developer happened to have
    configured globally, so it passed on a workstation and failed on a clean machine — found
    on a fresh Ubuntu, where `test_initializes_a_fresh_project` was the *only* failure in the
    whole run. These four variables override config and need no repository to exist yet,
    unlike `git config`, which has to run inside one.
    """
    for name, value in {
        "GIT_AUTHOR_NAME": "Kiln Test",
        "GIT_AUTHOR_EMAIL": "test@kiln.invalid",
        "GIT_COMMITTER_NAME": "Kiln Test",
        "GIT_COMMITTER_EMAIL": "test@kiln.invalid",
    }.items():
        monkeypatch.setenv(name, value)


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
        work_item=None,
    ):
        message_id = message_id or uuid.uuid4().hex
        with closing(db.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, sender, target, priority, status, content, created_at, branch,
                     work_item)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, sender, target, priority, status, content, created_at,
                    branch, work_item,
                ),
            )
            conn.commit()
        return message_id

    return _add


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def git_repo(tmp_path):
    """
    A real git repository with one commit.

    Real git rather than a fake runner: conflicts, empty squashes and detached anchors are
    exactly the cases a fake would model wrongly, and they are the ones that break.
    """
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "scheduler@kiln.test")
    _git(repo, "config", "user.name", "Kiln Scheduler Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


@pytest.fixture
def git_cmd():
    """Run a git command against a test repo, failing loudly on error."""
    return _git


@pytest.fixture
def read_message(db_path):
    """Fetch one message row back as a dict, for asserting on writer side effects."""

    def _read(message_id):
        with closing(db.connect(db_path)) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return dict(row) if row else None

    return _read
