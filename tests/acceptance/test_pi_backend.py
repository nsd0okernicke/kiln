"""Acceptance scenario: Pi crosses the real scheduler, Git, and SQLite boundaries."""

from workflow_support import git, prepare, rows, scheduler, send


def test_pi_completes_a_verified_handoff(initialized_project, command_runner, fake_pi):
    prepare(initialized_project, command_runner, agent_override="pi")
    commit = git(command_runner, initialized_project, "rev-parse", "HEAD").stdout.strip()
    inbound_id = send(command_runner, initialized_project, "implement with Pi", commit=commit)

    result = scheduler(
        command_runner,
        initialized_project,
        fake_pi,
        status="done",
        agent="pi",
    )

    assert "handed off to human-in-the-loop" in result.stderr
    worktree = initialized_project / ".worktrees" / "coder"
    assert (worktree / "system-worker.txt").read_text(encoding="utf-8") == (
        "written by deterministic Pi worker\n"
    )
    messages = rows(initialized_project)
    assert next(row for row in messages if row["id"].startswith(inbound_id))["status"] == (
        "processed"
    )
    assert messages[-1]["work_item"] == "system-test-task"
