"""Task-store invariants against real SQLite transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from kiln.scheduler.infrastructure.persistence import db, task_store

pytestmark = pytest.mark.integration


def test_tasks_are_unique_within_a_branch_and_independent_across_branches(db_path):
    task_store.create_task(db_path, branch="main", work_item="CAT-1", title="One", body="Main")

    with pytest.raises(task_store.TaskConflictError, match="already exists"):
        task_store.create_task(
            db_path, branch="main", work_item="CAT-1", title="Again", body="Duplicate"
        )
    other = task_store.create_task(
        db_path, branch="release", work_item="CAT-1", title="One", body="Release"
    )

    assert other["branch"] == "release"


def test_competing_handoffs_create_exactly_one_message(db_path):
    task = task_store.create_task(
        db_path, branch="main", work_item="CAT-2", title="Search", body="By author"
    )

    def dispatch():
        try:
            return task_store.handoff_task(
                db_path,
                branch="main",
                identifier=task["id"],
                sender="human-in-the-loop",
                target="specifier",
            )
        except task_store.TaskConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert len(db.work_item_messages(db_path, "main")) == 1


def test_archived_task_remains_queryable_but_leaves_backlog(db_path):
    task = task_store.create_task(
        db_path, branch="main", work_item="CAT-3", title="Later", body="Maybe"
    )

    task_store.archive_task(db_path, branch="main", identifier=task["id"])

    assert task_store.list_tasks(db_path, branch="main", status="backlog") == []
    assert task_store.get_task(db_path, branch="main", identifier="CAT-3")["status"] == "archived"
