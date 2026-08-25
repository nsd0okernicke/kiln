"""Acceptance scenario: complete one autonomous handoff."""

from workflow_support import git, prepare, rows, scheduler, send


def test_completes_one_verified_handoff(initialized_project, command_runner, fake_claude):
    prepare(initialized_project, command_runner)
    (initialized_project / "human-input.txt").write_text("incoming work\n", encoding="utf-8")
    git(command_runner, initialized_project, "add", "human-input.txt")
    git(command_runner, initialized_project, "commit", "-m", "Human input")
    commit = git(command_runner, initialized_project, "rev-parse", "HEAD").stdout.strip()
    inbound_id = send(command_runner, initialized_project, "implement it", commit=commit)

    result = scheduler(command_runner, initialized_project, fake_claude, status="done")

    assert "handed off to human-in-the-loop" in result.stderr
    assert "verification passed" in result.stderr
    worktree = initialized_project / ".worktrees" / "coder"
    assert (worktree / "human-input.txt").read_text(encoding="utf-8") == "incoming work\n"
    assert (worktree / "system-worker.txt").is_file()
    messages = rows(initialized_project)
    inbound = next(row for row in messages if row["id"].startswith(inbound_id))
    outbound = messages[-1]
    assert inbound["status"] == "processed"
    assert outbound["target"] == "human-in-the-loop"
    assert outbound["work_item"] == "system-test-task"
    assert "Commit:" in outbound["content"]
    subject = git(command_runner, worktree, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "[Coder] completed deterministic worker task"
