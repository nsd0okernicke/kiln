"""Acceptance scenario: resolve and prepare a profile."""

from workflow_support import prepare


def test_dry_run_prepares_a_consistent_profile(initialized_project, command_runner):
    prepare(initialized_project, command_runner)

    assert (initialized_project / ".worktrees" / "coder" / ".git").is_file()
    assert (initialized_project / ".claude" / "agents" / "coder-worker.md").is_file()
    assert (initialized_project / ".kiln" / "sessions").is_file()
    dry_run = "\n".join(
        (command_runner.report_dir / name).read_text(encoding="utf-8")
        for name in ("04-stdout.log", "04-stderr.log")
    )
    assert "profile: spike" in dry_run
    assert "[coder]" in dry_run
    assert "kiln.scheduler.infrastructure.cli.role_scheduler" in dry_run
    assert "'--route' 'coder=human-in-the-loop'" in dry_run
