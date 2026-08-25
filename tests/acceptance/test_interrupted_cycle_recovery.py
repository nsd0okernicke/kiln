"""Acceptance scenario: recover a message abandoned in processing."""

import sqlite3
from contextlib import closing

from workflow_support import prepare, rows, scheduler, send


def test_scheduler_replays_processing_message_after_restart(
    initialized_project, command_runner, fake_claude
):
    prepare(initialized_project, command_runner)
    inbound_id = send(command_runner, initialized_project, "resume interrupted work")
    with closing(sqlite3.connect(initialized_project / ".kiln" / "messages.db")) as connection:
        connection.execute(
            "UPDATE messages SET status='processing', delivered_at=CURRENT_TIMESTAMP "
            "WHERE id LIKE ?",
            (inbound_id + "%",),
        )
        connection.commit()

    result = scheduler(command_runner, initialized_project, fake_claude, status="done")

    messages = rows(initialized_project)
    original = next(row for row in messages if row["id"].startswith(inbound_id))
    outbound = [row for row in messages if row["sender"] == "coder"]
    assert "recovered message" in result.stderr
    assert original["status"] == "processed"
    assert len(outbound) == 1
    assert outbound[0]["work_item"] == "system-test-task"
