"""Acceptance scenario: recover from a failed scheduler cycle."""

from conftest import REPO_ROOT, console_script
from workflow_support import prepare, rows, scheduler, send


def test_recovers_a_failed_cycle_with_public_retry(
    initialized_project, command_runner, fake_claude
):
    prepare(initialized_project, command_runner)
    inbound_id = send(command_runner, initialized_project, "fail then recover")

    scheduler(command_runner, initialized_project, fake_claude, status="blocked")
    failed = next(row for row in rows(initialized_project) if row["id"].startswith(inbound_id))
    assert failed["status"] == "failed"
    assert "deterministic worker task" in failed["error"]

    retried = command_runner.run(
        console_script("kiln"),
        "retry",
        inbound_id,
        "--guidance",
        "the fixture is ready now",
        "--working-dir",
        initialized_project,
        cwd=REPO_ROOT,
    )
    assert "resumed" in retried.stdout
    scheduler(command_runner, initialized_project, fake_claude, status="done")

    messages = rows(initialized_project)
    original = next(row for row in messages if row["id"].startswith(inbound_id))
    assert original["status"] == "processed"
    assert any(
        row["target"] == "human-in-the-loop"
        and row["work_item"] == "system-test-task"
        and row["status"] == "queued"
        for row in messages
    )
