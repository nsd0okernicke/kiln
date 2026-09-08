"""Acceptance scenario: escalate an incoming Git merge conflict before delegation."""

from workflow_support import git, prepare, rows, scheduler, send


def test_merge_conflict_escalates_without_launching_worker(
    initialized_project, command_runner, fake_pi
):
    """A handoff whose commit conflicts with the shared branch stops before the worker runs.

    The conflict has to be between the *inbound commit* and the shared branch, not between the
    receiving worktree and the branch: `_merge_inbound` resets the worktree to the branch tip
    before merging, deliberately, so a stale worktree left by an earlier cycle cannot
    manufacture a conflict. What stays reachable — and what this covers — is the case that
    reset was written for: a role's commit meets a shared branch that moved while that role
    was working.
    """
    conflict = initialized_project / "shared.txt"
    conflict.write_text("base\n", encoding="utf-8")
    prepare(initialized_project, command_runner, profile="full")

    # The coder commits its own version, on its own branch...
    coder_worktree = initialized_project / ".worktrees" / "coder"
    (coder_worktree / "shared.txt").write_text("coder branch\n", encoding="utf-8")
    git(command_runner, coder_worktree, "add", "shared.txt")
    git(command_runner, coder_worktree, "commit", "-m", "Coder-side change")
    coder_commit = git(command_runner, coder_worktree, "rev-parse", "HEAD").stdout.strip()

    # ...while the shared branch moves underneath it, touching the same line.
    conflict.write_text("human branch\n", encoding="utf-8")
    git(command_runner, initialized_project, "add", "shared.txt")
    git(command_runner, initialized_project, "commit", "-m", "Human-side change")

    inbound_id = send(
        command_runner,
        initialized_project,
        "review this",
        target="reviewer",
        commit=coder_commit,
    )

    result = scheduler(
        command_runner,
        initialized_project,
        fake_pi,
        status="done",
        role="reviewer",
        target="architect",
    )

    messages = rows(initialized_project)
    original = next(row for row in messages if row["id"].startswith(inbound_id))
    assert "merge of" in result.stderr and "failed" in result.stderr
    assert original["status"] == "failed"
    assert "merge of" in original["error"]
    reviewer_worktree = initialized_project / ".worktrees" / "reviewer"
    assert not (reviewer_worktree / "system-worker.txt").exists()
    assert any(row["target"] == "human-in-the-loop" for row in messages)
