"""Persistence for human-owned backlog tasks."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import cast

from ...domain import handoff
from ...domain.models import DEFAULT_PRIORITY, MessageStatus
from .queue_storage import connect

TASK_BACKLOG = "backlog"
TASK_ACTIVE = "active"
TASK_ARCHIVED = "archived"


class TaskConflictError(Exception):
    """A task mutation could not be applied to its current state."""


def configure_context(
    db_path: str | Path, *, branch: str, human_role: str, intake_role: str
) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO task_context (branch, human_role, intake_role) VALUES (?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              human_role=excluded.human_role, intake_role=excluded.intake_role
            """,
            (branch, human_role, intake_role),
        )
        conn.commit()


def get_context(db_path: str | Path, *, branch: str) -> dict | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM task_context WHERE branch=?", (branch,)).fetchone()
    return _task(row) if row else None


def _task(row: sqlite3.Row) -> dict:
    return cast(dict, dict(row))


def create_task(db_path: str | Path, *, branch: str, work_item: str, title: str, body: str) -> dict:
    with closing(connect(db_path)) as conn:
        try:
            row = conn.execute(
                """
                INSERT INTO tasks (branch, work_item, title, body)
                VALUES (?, ?, ?, ?)
                RETURNING *
                """,
                (branch, work_item, title, body),
            ).fetchone()
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise TaskConflictError(
                f"task {work_item!r} already exists on branch {branch!r}"
            ) from exc
    return _task(row)


def list_tasks(db_path: str | Path, *, branch: str, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE branch=?"
    params: list[str] = [branch]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at, id"
    with closing(connect(db_path)) as conn:
        return [_task(row) for row in conn.execute(sql, params).fetchall()]


def get_task(db_path: str | Path, *, branch: str, identifier: str) -> dict | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE branch=? AND (id=? OR work_item=?)",
            (branch, identifier, identifier),
        ).fetchone()
    return _task(row) if row else None


def update_task(
    db_path: str | Path, *, branch: str, identifier: str, title: str, body: str
) -> dict:
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            UPDATE tasks SET title=?, body=?, updated_at=datetime('now', 'localtime')
            WHERE branch=? AND (id=? OR work_item=?) AND status=?
            RETURNING *
            """,
            (title, body, branch, identifier, identifier, TASK_BACKLOG),
        ).fetchone()
        conn.commit()
    if row is None:
        raise TaskConflictError(
            f"backlog task {identifier!r} was not found or is no longer editable"
        )
    return _task(row)


def archive_task(db_path: str | Path, *, branch: str, identifier: str) -> dict:
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            UPDATE tasks SET status=?, updated_at=datetime('now', 'localtime')
            WHERE branch=? AND (id=? OR work_item=?) AND status=?
            RETURNING *
            """,
            (TASK_ARCHIVED, branch, identifier, identifier, TASK_BACKLOG),
        ).fetchone()
        conn.commit()
    if row is None:
        raise TaskConflictError(f"backlog task {identifier!r} was not found or cannot be archived")
    return _task(row)


def handoff_task(
    db_path: str | Path,
    *,
    branch: str,
    identifier: str,
    sender: str,
    target: str,
    priority: int = DEFAULT_PRIORITY,
) -> dict:
    """Atomically queue one message and activate its backlog task."""
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE branch=? AND (id=? OR work_item=?)",
            (branch, identifier, identifier),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise TaskConflictError(f"task {identifier!r} was not found")
        task = _task(row)
        if task["status"] != TASK_BACKLOG:
            conn.rollback()
            raise TaskConflictError(f"task {task['work_item']!r} has already left the backlog")
        content = handoff.format_handoff(
            sender=sender,
            handoff=task["work_item"],
            branch=branch,
            commit="",
            summary=f"{task['title']}\n\n{task['body']}",
            next_role=target,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        message_id = str(
            conn.execute(
                """
                INSERT INTO messages
                  (sender, target, priority, status, content, created_at, branch, work_item)
                VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)
                RETURNING id
                """,
                (
                    sender,
                    target,
                    priority,
                    MessageStatus.QUEUED.value,
                    content,
                    branch,
                    task["work_item"],
                ),
            ).fetchone()[0]
        )
        activated = conn.execute(
            """
            UPDATE tasks
            SET status=?, message_id=?, dispatched_at=datetime('now', 'localtime'),
                updated_at=datetime('now', 'localtime')
            WHERE id=? AND status=?
            RETURNING *
            """,
            (TASK_ACTIVE, message_id, task["id"], TASK_BACKLOG),
        ).fetchone()
        if activated is None:  # defensive; BEGIN IMMEDIATE prevents a competing writer
            conn.rollback()
            raise TaskConflictError(f"task {task['work_item']!r} has already left the backlog")
        conn.commit()
    result = _task(activated)
    result["message_id"] = message_id
    return result
