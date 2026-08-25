"""Public HITL backlog CLI over the real SQLite task and message stores."""

from __future__ import annotations

import pytest

from kiln.scheduler.infrastructure.cli import task
from kiln.scheduler.infrastructure.persistence import db, task_store

pytestmark = pytest.mark.integration


def args(db_path, *command: str) -> list[str]:
    return ["--db-path", str(db_path), "--branch", "main", *command]


def test_cli_creates_lists_refines_and_archives_tasks(db_path, capsys):
    assert task.main(args(db_path, "create", "CAT-1", "--title", "Catalog", "--body", "Draft")) == 0
    assert task.main(args(db_path, "update", "CAT-1", "--body", "Ready")) == 0
    assert task.main(args(db_path, "list")) == 0
    assert '"work_item": "CAT-1"' in capsys.readouterr().out

    stored = task_store.get_task(db_path, branch="main", identifier="CAT-1")
    assert stored["body"] == "Ready"
    assert task.main(args(db_path, "archive", "CAT-1")) == 0
    assert task_store.get_task(db_path, branch="main", identifier="CAT-1")["status"] == "archived"


def test_cli_handoff_uses_the_projects_human_route(db_path, tmp_path):
    workflow = tmp_path / "kiln" / "project" / "constitution" / "workflow.md"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "## Handoff Routing\n\n| Role | Target | When Sender |\n"
        "| --- | --- | --- |\n| human-in-the-loop | specifier | |\n",
        encoding="utf-8",
    )
    assert (
        task.main(args(db_path, "create", "CAT-2", "--title", "Search", "--body", "By author")) == 0
    )

    result = task.main(
        [
            "--db-path",
            str(db_path),
            "--branch",
            "main",
            "--working-dir",
            str(tmp_path),
            "handoff",
            "CAT-2",
        ]
    )

    assert result == 0
    task_row = task_store.get_task(db_path, branch="main", identifier="CAT-2")
    message = db.get_message(db_path, task_row["message_id"])
    assert (message["target"], message["work_item"]) == ("specifier", "CAT-2")


def test_non_hitl_agent_session_cannot_mutate_backlog(db_path, monkeypatch, capsys):
    monkeypatch.setenv("KILN_ROLE", "coder")

    result = task.main(args(db_path, "create", "CAT-3", "--title", "No", "--body", "Forbidden"))

    assert result == 1
    assert "belongs to 'human-in-the-loop'" in capsys.readouterr().err
    assert task_store.list_tasks(db_path, branch="main") == []
