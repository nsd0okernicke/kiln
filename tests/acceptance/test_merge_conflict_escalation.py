"""Acceptance scenario: escalate an incoming Git merge conflict before delegation."""

from workflow_support import git, prepare, rows, scheduler, send


def test_merge_conflict_escalates_without_launching_worker(
    initialized_project, command_runner, fake_claude
):
    conflict = initialized_project / "shared.txt"
    conflict.write_text("base\n", encoding="utf-8")
    prepare(initialized_project, command_runner)

    worktree = initialized_project / ".worktrees" / "coder"
    (worktree / "shared.txt").write_text("coder branch\n", encoding="utf-8")
    git(command_runner, worktree, "add", "shared.txt")
    git(command_runner, worktree, "commit", "-m", "Coder-side change")

    conflict.write_text("human branch\n", encoding="utf-8")
    git(command_runner, initialized_project, "add", "shared.txt")
    git(command_runner, initialized_project, "commit", "-m", "Human-side change")
    commit = git(command_runner, initialized_project, "rev-parse", "HEAD").stdout.strip()
    inbound_id = send(command_runner, initialized_project, "merge this", commit=commit)

    result = scheduler(command_runner, initialized_project, fake_claude, status="done")

    messages = rows(initialized_project)
    original = next(row for row in messages if row["id"].startswith(inbound_id))
    assert "merge of" in result.stderr and "failed" in result.stderr
    assert original["status"] == "failed"
    assert "merge of" in original["error"]
    assert not (worktree / "system-worker.txt").exists()
    assert any(row["target"] == "human-in-the-loop" for row in messages)
