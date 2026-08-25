"""Acceptance scenario: recover after verification rejects completed worker output."""

from conftest import REPO_ROOT, console_script
from workflow_support import prepare, rows, scheduler, send


def test_recovers_after_verification_failure(initialized_project, command_runner, fake_claude):
    prepare(initialized_project, command_runner)
    inbound_id = send(command_runner, initialized_project, "verify then recover")

    failed_run = scheduler(
        command_runner,
        initialized_project,
        fake_claude,
        status="done",
        verification_status="fail",
    )
    failed = next(row for row in rows(initialized_project) if row["id"].startswith(inbound_id))
    assert "verification failed" in failed_run.stderr
    assert failed["status"] == "failed"
    assert "verification failed" in failed["error"]

    retry = command_runner.run(
        console_script("kiln"),
        "retry",
        inbound_id,
        "--guidance",
        "verification environment repaired",
        "--working-dir",
        initialized_project,
        cwd=REPO_ROOT,
    )
    assert "resumed" in retry.stdout
    successful_run = scheduler(
        command_runner,
        initialized_project,
        fake_claude,
        status="done",
        verification_status="pass",
    )

    messages = rows(initialized_project)
    original = next(row for row in messages if row["id"].startswith(inbound_id))
    assert "verification passed" in successful_run.stderr
    assert original["status"] == "processed"
    assert any(
        row["target"] == "human-in-the-loop" and row["work_item"] == "system-test-task"
        for row in messages
    )
