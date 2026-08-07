"""
Scheduler entry points and failure paths.

These cover the seams that only run when something has already gone wrong — a silently
failing INSERT, an unwritable debug file, a git command that dies — plus the CLI surface
that bin/kiln.ps1 and bin/kiln.sh will launch. They are the parts with no happy-path test
to exercise them, and therefore the parts most likely to break unnoticed.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

import pytest
from scheduler import db, git_ops, handoff, role_scheduler
from scheduler.role_scheduler import SchedulerContext, SchedulerState
from scheduler.routing import parse_routing_table
from scheduler.worker_prompt import WorkerDefinition
from test_role_scheduler import DEFINITION, ROUTING, FakeWorker, queued_for, worker

pytestmark = pytest.mark.integration

WORKER_FILE = """\
---
name: coder-worker
description: does the work
model: claude-sonnet-5
---

# Coder Role
"""


class TestInsertVerification:
    def test_retries_when_the_insert_is_not_visible(self, db_path, git_repo, monkeypatch):
        # kiln-handoff/SKILL.md step 5 exists because the INSERT has been seen to fail
        # silently; this proves the scheduler actually retries rather than assuming.
        calls = {"verify": 0}
        real_verify = db.verify_queued

        def flaky_verify(*args, **kwargs):
            calls["verify"] += 1
            return None if calls["verify"] == 1 else real_verify(*args, **kwargs)

        monkeypatch.setattr(role_scheduler.db, "verify_queued", flaky_verify)

        ctx = SchedulerContext(
            role="coder", branch="main", db_path=db_path, worktree=git_repo,
            routing=ROUTING, definition=DEFINITION, run_worker=FakeWorker(),
        )
        assert role_scheduler._insert_verified(ctx, "refactorer", "payload") is not None
        assert calls["verify"] == 2

    def test_gives_up_after_two_attempts(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(role_scheduler.db, "verify_queued", lambda *a, **k: None)
        ctx = SchedulerContext(
            role="coder", branch="main", db_path=db_path, worktree=git_repo,
            routing=ROUTING, definition=DEFINITION, run_worker=FakeWorker(),
        )
        assert role_scheduler._insert_verified(ctx, "refactorer", "payload") is None


class TestSquashFailureEscalates:
    def test_failed_squash_does_not_forward_work(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(
            role_scheduler.git_ops,
            "squash_since",
            lambda *a, **k: git_ops.GitResult(False, "", "disk full", 1),
        )
        content = handoff.format_handoff(
            sender="specifier", handoff="h", branch="main",
            commit=git_ops.head_commit(git_repo), summary="s",
            next_role="coder", timestamp="2026-08-07 10:00:00",
        )
        db.insert_handoff(db_path, "specifier", "coder", content, "main")

        ctx = SchedulerContext(
            role="coder", branch="main", db_path=db_path, worktree=git_repo,
            routing=ROUTING, definition=DEFINITION, run_worker=FakeWorker(worker()),
            clock=lambda: datetime(2026, 8, 7, 14, 0, 0),
        )
        result = role_scheduler.run_once(ctx, SchedulerState())

        assert result.outcome == role_scheduler.ESCALATED
        assert queued_for(db_path, "refactorer") == []
        assert "disk full" in queued_for(db_path, "human-in-the-loop")[0]["content"]


class TestPersistInboundIsNeverFatal:
    def test_unwritable_debug_file_does_not_fail_the_cycle(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(
            role_scheduler.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        ctx = SchedulerContext(
            role="coder", branch="main", db_path=db_path, worktree=git_repo,
            routing=ROUTING, definition=DEFINITION, run_worker=FakeWorker(),
        )
        role_scheduler._persist_inbound(ctx, "content")  # must not raise


class TestStatusWriterFailure:
    def test_subprocess_error_is_swallowed(self, tmp_path, monkeypatch):
        script = tmp_path / "set-status.py"
        script.write_text("", encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("cannot spawn")

        monkeypatch.setattr(role_scheduler.subprocess, "run", boom)
        role_scheduler.make_status_writer("coder", script)("working")  # must not raise

    def test_no_script_yields_a_noop(self):
        role_scheduler.make_status_writer("coder", None)("working")


class TestCli:
    def _args(self, tmp_path, **overrides):
        worker_file = tmp_path / "coder-worker.md"
        worker_file.write_text(WORKER_FILE, encoding="utf-8")
        workflow = tmp_path / "workflow.md"
        workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
        argv = [
            "--role", "coder",
            "--branch", "main",
            "--db-path", str(tmp_path / "messages.db"),
            "--worktree", str(tmp_path),
            "--workflow", str(workflow),
            "--worker-agent", str(worker_file),
        ]
        for key, value in overrides.items():
            argv += [f"--{key}", str(value)]
        return argv

    def test_parses_required_arguments(self, tmp_path):
        args = role_scheduler.parse_args(self._args(tmp_path))
        assert args.role == "coder"
        assert args.branch == "main"
        assert args.agent == "claude"
        assert args.poll_interval == role_scheduler.DEFAULT_POLL_INTERVAL_SEC

    def test_missing_required_argument_exits(self):
        with pytest.raises(SystemExit):
            role_scheduler.parse_args(["--role", "coder"])

    def test_builds_a_context_from_arguments(self, tmp_path):
        ctx = role_scheduler.build_context(role_scheduler.parse_args(self._args(tmp_path)))
        assert isinstance(ctx, SchedulerContext)
        assert ctx.role == "coder"
        assert ctx.definition.name == "coder-worker"
        assert ctx.routing.resolve("coder") == "refactorer"

    def test_model_falls_back_to_the_worker_definition(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run_worker(**kwargs):
            captured.update(kwargs)
            return worker()

        monkeypatch.setattr(
            "scheduler.adapters.claude_adapter.run_worker", fake_run_worker
        )
        ctx = role_scheduler.build_context(role_scheduler.parse_args(self._args(tmp_path)))
        ctx.run_worker(prompt="p")
        assert captured["model"] == "claude-sonnet-5"

    def test_explicit_model_overrides_the_definition(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.claude_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path, model="opus"))
        role_scheduler.build_context(args).run_worker(prompt="p")
        assert captured["model"] == "opus"

    def test_once_mode_runs_a_single_cycle_and_exits(self, tmp_path, monkeypatch):
        db.ensure_schema(tmp_path / "messages.db")
        cycles = {"count": 0}

        def one_idle_cycle(ctx, state):
            cycles["count"] += 1
            return role_scheduler.CycleResult(role_scheduler.IDLE)

        monkeypatch.setattr(role_scheduler, "run_once", one_idle_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))

        assert role_scheduler.main([*self._args(tmp_path), "--once"]) == 0
        assert cycles["count"] == 1

    def test_halted_scheduler_exits_nonzero(self, tmp_path, monkeypatch):
        def halting_cycle(ctx, state):
            state.halted = True
            return role_scheduler.CycleResult(role_scheduler.ESCALATED, detail="boom")

        monkeypatch.setattr(role_scheduler, "run_once", halting_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))

        # A halted role must surface a non-zero exit so the pane shows it stopped.
        assert role_scheduler.main(self._args(tmp_path)) == 1

    def test_sleeps_only_when_idle(self, tmp_path, monkeypatch):
        outcomes = [role_scheduler.IDLE, role_scheduler.HANDED_OFF]
        sleeps = []

        def cycles(ctx, state):
            if not outcomes:
                state.halted = True
                return role_scheduler.CycleResult(role_scheduler.ESCALATED)
            return role_scheduler.CycleResult(outcomes.pop(0))

        monkeypatch.setattr(role_scheduler, "run_once", cycles)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))
        monkeypatch.setattr(role_scheduler.time, "sleep", sleeps.append)

        role_scheduler.main(self._args(tmp_path, **{"poll-interval": 0.01}))
        assert sleeps == [0.01], "must not sleep after a productive cycle"


def _dummy_ctx(tmp_path):
    return SchedulerContext(
        role="coder",
        branch="main",
        db_path=tmp_path / "messages.db",
        worktree=tmp_path,
        routing=parse_routing_table("| coder | refactorer |"),
        definition=WorkerDefinition(name="coder-worker", description="d", prompt="b"),
        run_worker=FakeWorker(),
    )


class TestGitFailurePaths:
    def test_timeout_is_reported_not_raised(self, git_repo, monkeypatch):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(git_ops.subprocess, "run", timeout)
        result = git_ops.run_git(["status"], git_repo)
        assert result.ok is False
        assert "timed out" in result.stderr

    def test_squash_reports_a_failed_reset(self, git_repo, monkeypatch):
        real = git_ops.run_git

        def fail_reset(args, cwd, **kwargs):
            if args[:1] == ["reset"]:
                return git_ops.GitResult(False, "", "reset refused", 1)
            return real(args, cwd, **kwargs)

        (git_repo / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(git_ops, "run_git", fail_reset)
        result = git_ops.squash_since("HEAD", "[Coder] x", git_repo)
        assert result.ok is False
        assert "reset refused" in result.stderr

    def test_squash_reports_a_failed_commit(self, git_repo, monkeypatch):
        real = git_ops.run_git

        def fail_commit(args, cwd, **kwargs):
            if args[:1] == ["commit"]:
                return git_ops.GitResult(False, "", "commit refused", 1)
            return real(args, cwd, **kwargs)

        (git_repo / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(git_ops, "run_git", fail_commit)
        assert git_ops.squash_since("HEAD", "[Coder] x", git_repo).ok is False

    def test_squash_reports_a_failed_stage(self, git_repo, monkeypatch):
        real = git_ops.run_git

        def fail_add(args, cwd, **kwargs):
            if args[:1] == ["add"]:
                return git_ops.GitResult(False, "", "add refused", 1)
            return real(args, cwd, **kwargs)

        (git_repo / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(git_ops, "run_git", fail_add)
        assert git_ops.squash_since("HEAD", "[Coder] x", git_repo).ok is False

    def test_commit_all_reports_a_failed_stage(self, git_repo, monkeypatch):
        real = git_ops.run_git

        def fail_add(args, cwd, **kwargs):
            if args[:1] == ["add"]:
                return git_ops.GitResult(False, "", "add refused", 1)
            return real(args, cwd, **kwargs)

        (git_repo / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(git_ops, "run_git", fail_add)
        assert git_ops.commit_all("msg", git_repo).ok is False

    def test_commit_all_reports_a_failed_commit(self, git_repo, monkeypatch):
        real = git_ops.run_git

        def fail_commit(args, cwd, **kwargs):
            if args[:1] == ["commit"]:
                return git_ops.GitResult(False, "", "commit refused", 1)
            return real(args, cwd, **kwargs)

        (git_repo / "x.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(git_ops, "run_git", fail_commit)
        result = git_ops.commit_all("msg", git_repo)
        assert result.ok is False
        assert "commit refused" in result.stderr


class TestEnsureIgnored:
    def test_adds_the_pattern_to_local_exclude(self, git_repo):
        assert git_ops.is_ignored("tmp/", git_repo) is False
        git_ops.ensure_ignored("tmp/", git_repo)
        assert git_ops.is_ignored("tmp/", git_repo) is True

    def test_is_idempotent(self, git_repo):
        git_ops.ensure_ignored("tmp/", git_repo)
        git_ops.ensure_ignored("tmp/", git_repo)
        exclude = git_repo / ".git" / "info" / "exclude"
        assert exclude.read_text(encoding="utf-8").count("tmp/") == 1

    def test_respects_an_existing_gitignore(self, git_repo, git_cmd):
        (git_repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "ignore tmp")

        git_ops.ensure_ignored("tmp/", git_repo)

        exclude = git_repo / ".git" / "info" / "exclude"
        content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        assert "tmp/" not in content, "already ignored; must not touch the exclude file"

    def test_unresolvable_git_dir_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            git_ops, "run_git", lambda *a, **k: git_ops.GitResult(False, "", "not a repo", 1)
        )
        git_ops.ensure_ignored("tmp/", tmp_path)  # must not raise

    def test_unwritable_exclude_is_not_fatal(self, git_repo, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(git_ops.Path, "write_text", boom)
        git_ops.ensure_ignored("tmp/", git_repo)  # must not raise
