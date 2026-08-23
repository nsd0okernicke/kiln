"""
Scheduler entry points and failure paths.

These cover the seams that only run when something has already gone wrong — a silently
failing INSERT, an unwritable debug file, a git command that dies — plus the CLI surface
that bin/kiln.ps1 and bin/kiln.sh will launch. They are the parts with no happy-path test
to exercise them, and therefore the parts most likely to break unnoticed.
"""

from __future__ import annotations

import io
import subprocess
from contextlib import closing
from datetime import datetime

import pytest
from scheduler import db, git_ops, handoff, infrastructure, pane_status, role_scheduler
from scheduler.adapters import TokenUsage
from scheduler.infrastructure import CallableWorkerRunner, GitWorktree, SQLiteMessageQueue
from scheduler.models import WorkerRequest
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
        real_verify = db.message_exists

        def flaky_verify(*args, **kwargs):
            calls["verify"] += 1
            return False if calls["verify"] == 1 else real_verify(*args, **kwargs)

        monkeypatch.setattr(infrastructure.db, "message_exists", flaky_verify)

        ctx = SchedulerContext(
            role="coder",
            branch="main",
            db_path=db_path,
            worktree=git_repo,
            routing=ROUTING,
            definition=DEFINITION,
            worker_runner=CallableWorkerRunner(FakeWorker()),
            queue=SQLiteMessageQueue(db_path),
            worktree_port=GitWorktree(git_repo),
        )
        assert role_scheduler._insert_verified(ctx, "refactorer", "payload") is not None
        assert calls["verify"] == 2

    def test_a_consumer_taking_the_message_does_not_cause_a_duplicate(self, db_path, git_repo):
        # The live failure, reproduced: the receiving role polls every couple of seconds and
        # can take the handoff one second after it is written. A verification that asked "is
        # there a *queued* message from me?" then reported the insert had failed, and the
        # sender sent the whole handoff again -- one request, two full specifier cycles.
        ctx = SchedulerContext(
            role="coder",
            branch="main",
            db_path=db_path,
            worktree=git_repo,
            routing=ROUTING,
            definition=DEFINITION,
            worker_runner=CallableWorkerRunner(FakeWorker()),
            queue=SQLiteMessageQueue(db_path),
            worktree_port=GitWorktree(git_repo),
        )
        original_insert = db.insert_handoff

        def insert_then_consume(*args, **kwargs):
            message_id = original_insert(*args, **kwargs)
            db.fetch_and_deliver(db_path, "refactorer", "main")  # the consumer, racing
            return message_id

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(infrastructure.db, "insert_handoff", insert_then_consume)
        try:
            assert role_scheduler._insert_verified(ctx, "refactorer", "payload") is not None
        finally:
            monkeypatch.undo()

        with closing(db.connect(db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert total == 1, "the handoff must be sent exactly once"

    def test_gives_up_after_two_attempts(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(infrastructure.db, "message_exists", lambda *a, **k: False)
        ctx = SchedulerContext(
            role="coder",
            branch="main",
            db_path=db_path,
            worktree=git_repo,
            routing=ROUTING,
            definition=DEFINITION,
            worker_runner=CallableWorkerRunner(FakeWorker()),
            queue=SQLiteMessageQueue(db_path),
            worktree_port=GitWorktree(git_repo),
        )
        assert role_scheduler._insert_verified(ctx, "refactorer", "payload") is None


class TestSquashFailureEscalates:
    def test_failed_squash_does_not_forward_work(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(
            infrastructure.git_ops,
            "squash_since",
            lambda *a, **k: git_ops.GitResult(False, "", "disk full", 1),
        )
        content = handoff.format_handoff(
            sender="specifier",
            handoff="h",
            branch="main",
            commit=git_ops.head_commit(git_repo),
            summary="s",
            next_role="coder",
            timestamp="2026-08-07 10:00:00",
        )
        db.insert_handoff(db_path, "specifier", "coder", content, "main")

        ctx = SchedulerContext(
            role="coder",
            branch="main",
            db_path=db_path,
            worktree=git_repo,
            routing=ROUTING,
            definition=DEFINITION,
            # The worker must genuinely change something, or the cycle ends as a no-op
            # before it ever reaches the squash this test is about.
            worker_runner=CallableWorkerRunner(
                FakeWorker(worker(), edits_file=git_repo / "work.txt")
            ),
            queue=SQLiteMessageQueue(db_path),
            worktree_port=GitWorktree(git_repo),
            clock=lambda: datetime(2026, 8, 7, 14, 0, 0),
        )
        result = role_scheduler.run_once(ctx, SchedulerState())

        assert result.outcome == role_scheduler.ESCALATED
        assert queued_for(db_path, "refactorer") == []
        assert "disk full" in queued_for(db_path, "human-in-the-loop")[0]["content"]


class TestPersistInboundIsNeverFatal:
    def test_unwritable_debug_file_does_not_fail_the_cycle(self, db_path, git_repo, monkeypatch):
        monkeypatch.setattr(
            infrastructure.Path,
            "mkdir",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        ctx = SchedulerContext(
            role="coder",
            branch="main",
            db_path=db_path,
            worktree=git_repo,
            routing=ROUTING,
            definition=DEFINITION,
            worker_runner=CallableWorkerRunner(FakeWorker()),
            queue=SQLiteMessageQueue(db_path),
            worktree_port=GitWorktree(git_repo),
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
            "--role",
            "coder",
            "--branch",
            "main",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
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

        monkeypatch.setattr("scheduler.adapters.claude_adapter.run_worker", fake_run_worker)
        ctx = role_scheduler.build_context(role_scheduler.parse_args(self._args(tmp_path)))
        ctx.worker_runner(WorkerRequest(prompt="p"))
        assert captured["model"] == "claude-sonnet-5"

    def test_explicit_model_overrides_the_definition(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.claude_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path, model="opus"))
        role_scheduler.build_context(args).worker_runner(WorkerRequest(prompt="p"))
        assert captured["model"] == "opus"

    def test_worker_output_is_wired_to_an_emitter(self, tmp_path, monkeypatch):
        # So the pane can tell "the worker talking" apart from the scheduler's own log
        # lines -- see pane_status.tint_worker_output.
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.claude_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path))
        role_scheduler.build_context(args).worker_runner(WorkerRequest(prompt="p"))
        assert callable(captured["on_output"])


class TestAgentDispatch:
    """`--agent` must route to the matching adapter, not just be accepted and ignored."""

    def _args(self, tmp_path, worker_file, agent, **overrides):
        workflow = tmp_path / "workflow.md"
        workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
        argv = [
            "--role",
            "coder",
            "--branch",
            "main",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
            "--agent",
            agent,
        ]
        for key, value in overrides.items():
            argv += [f"--{key}", str(value)]
        return argv

    def test_copilot_agent_dispatches_to_the_copilot_adapter(self, tmp_path, monkeypatch):
        worker_file = tmp_path / "coder-worker.agent.md"
        worker_file.write_text(WORKER_FILE, encoding="utf-8")
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.copilot_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path, worker_file, "copilot"))
        role_scheduler.build_context(args).worker_runner(WorkerRequest(prompt="p"))
        assert captured["definition"].name == "coder-worker"

    def test_codex_agent_dispatches_to_the_codex_adapter(self, tmp_path, monkeypatch):
        worker_file = tmp_path / "coder-worker.toml"
        worker_file.write_text(
            'name = "coder-worker"\n'
            'description = "does the work"\n'
            "mcp_servers = {}\n"
            "developer_instructions = '''\n# Coder Role\n'''\n",
            encoding="utf-8",
        )
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.codex_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path, worker_file, "codex"))
        role_scheduler.build_context(args).worker_runner(WorkerRequest(prompt="p"))
        assert captured["definition"].name == "coder-worker"

    def test_grok_agent_dispatches_to_the_grok_adapter(self, tmp_path, monkeypatch):
        worker_file = tmp_path / "coder-worker.md"
        worker_file.write_text(WORKER_FILE, encoding="utf-8")
        captured = {}
        monkeypatch.setattr(
            "scheduler.adapters.grok_adapter.run_worker",
            lambda **kwargs: captured.update(kwargs) or worker(),
        )
        args = role_scheduler.parse_args(self._args(tmp_path, worker_file, "grok"))
        role_scheduler.build_context(args).worker_runner(WorkerRequest(prompt="p"))
        assert captured["definition"].name == "coder-worker"


class TestResolveModel:
    """Only Claude has a model called 'sonnet' -- the fallback must not leak to the others."""

    def _args(self, tmp_path, agent, model=""):
        worker_file = tmp_path / "coder-worker.md"
        worker_file.write_text(
            WORKER_FILE.replace("model: claude-sonnet-5\n", ""), encoding="utf-8"
        )
        workflow = tmp_path / "workflow.md"
        workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
        argv = [
            "--role",
            "coder",
            "--branch",
            "main",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
            "--agent",
            agent,
        ]
        if model:
            argv += ["--model", model]
        return role_scheduler.parse_args(argv)

    def test_claude_falls_back_to_sonnet(self, tmp_path):
        args = self._args(tmp_path, "claude")
        definition = role_scheduler.load_worker_definition(args.worker_agent)
        assert role_scheduler.resolve_model(args, definition) == "sonnet"

    @pytest.mark.parametrize("agent", ["copilot", "codex", "grok"])
    def test_other_backends_fall_back_to_no_flag_at_all(self, tmp_path, agent):
        # Empty string means "the CLI picks its own default" -- "sonnet" is not a model name
        # any of these backends recognises.
        args = self._args(tmp_path, agent)
        definition = role_scheduler.load_worker_definition(args.worker_agent)
        assert role_scheduler.resolve_model(args, definition) == ""

    def test_an_explicit_flag_still_wins_for_any_backend(self, tmp_path):
        args = self._args(tmp_path, "codex", model="o3")
        definition = role_scheduler.load_worker_definition(args.worker_agent)
        assert role_scheduler.resolve_model(args, definition) == "o3"


class TestWorkerOutputEmitter:
    def test_tints_lines_when_the_pane_is_a_terminal(self, monkeypatch, capsys):
        monkeypatch.setattr(role_scheduler.sys.stdout, "isatty", lambda: True)
        role_scheduler._make_worker_output_emitter()("hello")
        assert capsys.readouterr().out == pane_status.tint_worker_output("hello") + "\n"

    def test_stays_plain_when_output_is_piped(self, monkeypatch, capsys):
        monkeypatch.setattr(role_scheduler.sys.stdout, "isatty", lambda: False)
        role_scheduler._make_worker_output_emitter()("hello")
        assert capsys.readouterr().out == "hello\n"

    def test_output_is_also_appended_to_the_worker_log(self, tmp_path, capsys):
        target = tmp_path / "logs" / "worker-coder.log"

        role_scheduler._make_worker_output_emitter(target)("hello")

        assert capsys.readouterr().out == "hello\n"
        assert target.read_text(encoding="utf-8") == "hello\n"

    def test_an_unwritable_log_does_not_stop_output(self, tmp_path, capsys):
        parent_is_file = tmp_path / "not-a-directory"
        parent_is_file.write_text("x", encoding="utf-8")
        emit = role_scheduler._make_worker_output_emitter(parent_is_file / "worker.log")

        emit("first")
        emit("second")

        assert capsys.readouterr().out == "first\nsecond\n"


class TestStartupBanner:
    """
    What the pane shows on launch.

    Previously the first thing an operator saw was the echoed `python -m
    scheduler.role_scheduler --role ... --db-path ...` line: every fact present, none of
    them readable.
    """

    def _banner(self, tmp_path, workflow_text="| coder | refactorer |\n", **overrides):
        worker_file = tmp_path / "coder-worker.md"
        worker_file.write_text(WORKER_FILE, encoding="utf-8")
        workflow = tmp_path / "workflow.md"
        workflow.write_text(workflow_text, encoding="utf-8")
        argv = [
            "--role",
            "coder",
            "--branch",
            "feature-x",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
        ]
        for key, value in overrides.items():
            argv += [f"--{key}", str(value)]
        args = role_scheduler.parse_args(argv)
        return "\n".join(role_scheduler.format_banner(role_scheduler.build_context(args), args))

    def test_shows_the_role_and_branch(self, tmp_path):
        banner = self._banner(tmp_path)
        assert "role" in banner
        assert "feature-x" in banner

    def test_shows_the_resolved_worker_and_model(self, tmp_path):
        # The model is resolved from three sources; the pane must show which one won.
        assert "coder-worker" in self._banner(tmp_path)
        assert "claude-sonnet-5" in self._banner(tmp_path)

    def test_an_explicit_model_is_what_gets_shown(self, tmp_path):
        assert "opus" in self._banner(tmp_path, model="opus")

    def test_an_unset_model_reads_as_a_deliberate_default_not_blank(self, tmp_path):
        # Observed live: a copilot role with no configured model rendered "coder-worker
        # (copilot )" -- a trailing blank that looks like broken config, not "the CLI picks
        # its own default".
        worker_file = tmp_path / "coder-worker.agent.md"
        worker_file.write_text(
            WORKER_FILE.replace("model: claude-sonnet-5\n", ""), encoding="utf-8"
        )
        workflow = tmp_path / "workflow.md"
        workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
        argv = [
            "--role",
            "coder",
            "--branch",
            "main",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
            "--agent",
            "copilot",
        ]
        args = role_scheduler.parse_args(argv)
        banner = "\n".join(role_scheduler.format_banner(role_scheduler.build_context(args), args))
        assert "(CLI default)" in banner
        assert "copilot )" not in banner

    def test_shows_where_handoffs_will_go(self, tmp_path):
        # Routing is the single most surprising piece of config; showing it makes a
        # misrouted handoff diagnosable before it happens rather than after.
        assert "refactorer" in self._banner(tmp_path)

    def test_conditional_routes_name_their_condition(self, tmp_path):
        banner = self._banner(
            tmp_path,
            workflow_text="| coder | refactorer |\n| coder | human-in-the-loop | architect |\n",
        )
        assert "human-in-the-loop" in banner
        assert "architect" in banner

    def test_a_role_with_no_route_says_so(self, tmp_path):
        # Silence here would look identical to a working config until the first handoff.
        assert "no route" in self._banner(tmp_path, workflow_text="| specifier | coder |\n")

    def test_omits_the_log_line_when_no_log_file_is_configured(self, tmp_path):
        assert "log " not in self._banner(tmp_path)

    def test_shows_the_log_file_when_configured(self, tmp_path):
        assert "run.log" in self._banner(tmp_path, **{"log-file": tmp_path / "run.log"})

    def test_does_not_leak_the_raw_command_line(self, tmp_path):
        assert "--db-path" not in self._banner(tmp_path)


class TestTuningFlags:
    """
    `max_attempts` and `escalation_limit` were `SchedulerContext` dataclass defaults with no
    CLI flag at all -- changeable only from code, despite being the two numbers that decide
    how an unattended swarm gives up.
    """

    _args = TestCli._args

    def test_defaults_match_the_dataclass(self, tmp_path):
        args = role_scheduler.parse_args(self._args(tmp_path))
        assert args.max_attempts == role_scheduler.SchedulerContext.max_attempts
        assert args.escalation_limit == role_scheduler.SchedulerContext.escalation_limit

    def test_they_reach_the_context(self, tmp_path):
        db.ensure_schema(tmp_path / "messages.db")
        args = role_scheduler.parse_args(
            [*self._args(tmp_path), "--max-attempts", "5", "--escalation-limit", "1"]
        )
        ctx = role_scheduler.build_context(args)
        assert ctx.max_attempts == 5
        assert ctx.escalation_limit == 1


class TestCliLoop:
    _args = TestCli._args

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

    def test_a_halted_scheduler_exits_nonzero_under_once(self, tmp_path, monkeypatch):
        # A halted role no longer exits during a normal run -- it parks and polls so
        # `kiln retry` can reach it (see test_scheduler_resilience.TestHaltedLoopParks).
        # Under `--once` it must still exit, and non-zero: a scripted single cycle cannot
        # block forever waiting for a human to type a command.
        def halting_cycle(ctx, state):
            state.halted = True
            return role_scheduler.CycleResult(role_scheduler.ESCALATED, detail="boom")

        monkeypatch.setattr(role_scheduler, "run_once", halting_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))

        assert role_scheduler.main([*self._args(tmp_path), "--once"]) == 1

    def test_halted_scheduler_reports_through_ctx_set_status(self, tmp_path, monkeypatch):
        # Regression: `_run_loop` used to call `bar.update(state="halted")` directly,
        # which only ever reached this pane's own bottom row. `.kiln/status/<role>.json` --
        # the file that drives the WezTerm tab-bar badge -- is written by `ctx.set_status`,
        # not by the bar, so a direct `bar.update` left the badge silently stuck on
        # whatever state was last written successfully.
        seen = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **_kwargs: seen.append(state)

        def halting_cycle(ctx, state):
            state.halted = True
            return role_scheduler.CycleResult(role_scheduler.ESCALATED, detail="boom")

        monkeypatch.setattr(role_scheduler, "run_once", halting_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: ctx)

        assert role_scheduler.main([*self._args(tmp_path), "--once"]) == 1
        assert seen[-1] == "halted"

    def test_repeated_cycle_failures_report_blocked_then_halted(self, tmp_path, monkeypatch):
        # Same regression as above, for the other bypass site: the exception handler's
        # "blocked" during retries, then "halted" once MAX_CONSECUTIVE_ERRORS is reached.
        seen = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **_kwargs: seen.append(state)

        def always_fails(ctx, state):
            raise RuntimeError("boom")

        monkeypatch.setattr(role_scheduler, "run_once", always_fails)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: ctx)
        monkeypatch.setattr(role_scheduler.time, "sleep", lambda _seconds: None)

        assert role_scheduler.main(self._args(tmp_path)) == 1
        assert seen.count("blocked") == role_scheduler.MAX_CONSECUTIVE_ERRORS
        assert seen[-1] == "halted"

    def test_sleeps_only_when_idle(self, tmp_path, monkeypatch):
        outcomes = [role_scheduler.IDLE, role_scheduler.HANDED_OFF]
        sleeps = []

        def cycles(ctx, state):
            if not outcomes:
                # Ends the loop without halting: a halted role now parks and keeps polling,
                # which would sleep forever here instead of stopping.
                raise KeyboardInterrupt
            return role_scheduler.CycleResult(outcomes.pop(0))

        monkeypatch.setattr(role_scheduler, "run_once", cycles)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))
        monkeypatch.setattr(role_scheduler.time, "sleep", sleeps.append)

        role_scheduler.main(self._args(tmp_path, **{"poll-interval": 0.01}))
        assert sleeps == [0.01], "must not sleep after a productive cycle"


class TestStatusBarWiring:
    """The bar must reflect the cycle without any transition having to remember it."""

    _args = TestCli._args

    def _bar(self, tmp_path, **overrides):
        args = role_scheduler.parse_args(self._args(tmp_path, **overrides))
        return role_scheduler.attach_status_bar(_dummy_ctx(tmp_path), args), args

    def test_set_status_still_reaches_the_original_writer(self, tmp_path):
        # The JSON file drives the WezTerm tab-bar badges; the pane bar is additive.
        written = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **kwargs: written.append((state, kwargs))
        args = role_scheduler.parse_args(self._args(tmp_path))

        bar = role_scheduler.attach_status_bar(ctx, args)
        ctx.set_status("working")

        assert written[0][0] == "working"
        assert bar.status.state == "working"

    def test_current_cycles_cost_and_tokens_are_threaded_through(self, tmp_path):
        # The dashboard's swarm-wide totals read cycles/cost_usd/tokens straight out of the
        # JSON set-status.py writes -- this is what actually gets them there, on every state
        # change, from the same bar.status the pane's own bottom row already tracks.
        written = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **kwargs: written.append((state, kwargs))
        args = role_scheduler.parse_args(self._args(tmp_path))

        bar = role_scheduler.attach_status_bar(ctx, args)
        usage = TokenUsage(input_tokens=200, cache_read_tokens=4000)
        role_scheduler._record_cycle(
            bar,
            role_scheduler.CycleResult(role_scheduler.HANDED_OFF, cost_usd=1.5, tokens=usage),
        )
        ctx.set_status("working")

        _, kwargs = written[-1]
        assert kwargs == {"cycles": 1, "cost_usd": 1.5, "tokens": usage}

    def test_the_status_file_does_not_lag_the_bar_by_a_cycle(self, tmp_path, monkeypatch):
        # `_record_cycle` folds a cycle into the bar *after* run_once returns, but the last
        # set_status of that cycle ("idle") runs inside it -- so the status file the dashboard
        # reads carried the previous cycle's totals, and the two surfaces disagreed about the
        # same number.
        written = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **kwargs: written.append(kwargs)

        def one_cycle(inner_ctx, state):
            inner_ctx.set_status("idle")  # what _hand_off does at the end of a cycle
            return role_scheduler.CycleResult(role_scheduler.HANDED_OFF, cost_usd=2.0)

        monkeypatch.setattr(role_scheduler, "run_once", one_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: ctx)

        role_scheduler.main([*self._args(tmp_path), "--once"])

        assert written[-1]["cycles"] == 1
        assert written[-1]["cost_usd"] == pytest.approx(2.0)

    def test_an_idle_poll_does_not_rewrite_the_status_file(self, tmp_path, monkeypatch):
        # Otherwise every poll of an idle swarm rewrites N status files for no change.
        written = []
        ctx = _dummy_ctx(tmp_path)
        ctx.set_status = lambda state, **kwargs: written.append(kwargs)

        monkeypatch.setattr(
            role_scheduler,
            "run_once",
            lambda c, s: role_scheduler.CycleResult(role_scheduler.IDLE),
        )
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: ctx)

        role_scheduler.main([*self._args(tmp_path), "--once"])

        assert written == []

    def test_the_handoff_target_is_shown_before_the_first_cycle(self, tmp_path):
        bar, _ = self._bar(tmp_path)
        assert bar.status.target == "refactorer"

    def test_can_be_turned_off(self, tmp_path):
        args = role_scheduler.parse_args([*self._args(tmp_path), "--no-status-bar"])
        bar = role_scheduler.attach_status_bar(_dummy_ctx(tmp_path), args)
        assert bar.enabled is False

    def test_idle_polls_are_not_counted_as_cycles(self, tmp_path):
        # An idle scheduler would otherwise show a cycle count climbing every two seconds.
        bar, _ = self._bar(tmp_path)
        role_scheduler._record_cycle(bar, role_scheduler.CycleResult(role_scheduler.IDLE))
        assert bar.status.cycles == 0

    def test_productive_cycles_accumulate(self, tmp_path):
        bar, _ = self._bar(tmp_path)
        for _ in range(2):
            role_scheduler._record_cycle(
                bar,
                role_scheduler.CycleResult(
                    role_scheduler.HANDED_OFF, target="coder", detail="done", cost_usd=0.5
                ),
            )
        assert bar.status.cycles == 2
        assert bar.status.cost_usd == pytest.approx(1.0)
        assert bar.status.target == "coder"

    def test_a_long_summary_is_cut_down_to_bar_size(self, tmp_path):
        # Worker summaries run to several sentences; unclipped they would push out the
        # state and counters, which are the fields that matter.
        bar, _ = self._bar(tmp_path)
        role_scheduler._record_cycle(
            bar, role_scheduler.CycleResult(role_scheduler.HANDED_OFF, detail="word " * 200)
        )
        assert len(bar.status.detail) <= role_scheduler.MAX_BAR_DETAIL_CHARS

    def test_the_scrolling_region_is_released_when_the_loop_ends(self, tmp_path, monkeypatch):
        # A region that outlives the process leaves the pane's shell behaving strangely.
        from scheduler import pane_status

        class FakeTty(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTty()
        bar = pane_status.StatusBar(pane_status.PaneStatus(role="coder"), stream=stream)

        def halting_cycle(ctx, state):
            state.halted = True
            return role_scheduler.CycleResult(role_scheduler.ESCALATED)

        monkeypatch.setattr(role_scheduler, "run_once", halting_cycle)
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))
        monkeypatch.setattr(role_scheduler, "attach_status_bar", lambda ctx, args: bar)

        assert role_scheduler.main([*self._args(tmp_path), "--once"]) == 1
        assert pane_status.RESET_REGION in stream.getvalue()

    def test_the_region_is_released_even_when_the_loop_raises(self, tmp_path, monkeypatch):
        from scheduler import pane_status

        class FakeTty(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTty()
        bar = pane_status.StatusBar(pane_status.PaneStatus(role="coder"), stream=stream)

        monkeypatch.setattr(
            role_scheduler,
            "_run_loop",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monkeypatch.setattr(role_scheduler, "build_context", lambda args: _dummy_ctx(tmp_path))
        monkeypatch.setattr(role_scheduler, "attach_status_bar", lambda ctx, args: bar)

        with pytest.raises(RuntimeError):
            role_scheduler.main(self._args(tmp_path))
        assert pane_status.RESET_REGION in stream.getvalue()


def _dummy_ctx(tmp_path):
    return SchedulerContext(
        role="coder",
        branch="main",
        db_path=tmp_path / "messages.db",
        worktree=tmp_path,
        routing=parse_routing_table("| coder | refactorer |"),
        definition=WorkerDefinition(name="coder-worker", description="d", prompt="b"),
        worker_runner=CallableWorkerRunner(FakeWorker()),
        queue=SQLiteMessageQueue(tmp_path / "messages.db"),
        worktree_port=GitWorktree(tmp_path),
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


class TestRouteOverride:
    """
    A profile that declares its own routing replaces workflow.md's table outright. Partial
    overlay would mean answering "where does this role hand off" requires reading two files
    and knowing which wins.
    """

    def _args(self, tmp_path, *routes):
        worker_file = tmp_path / "coder-worker.md"
        worker_file.write_text(WORKER_FILE, encoding="utf-8")
        workflow = tmp_path / "workflow.md"
        workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
        argv = [
            "--role",
            "coder",
            "--branch",
            "main",
            "--db-path",
            str(tmp_path / "messages.db"),
            "--worktree",
            str(tmp_path),
            "--workflow",
            str(workflow),
            "--worker-agent",
            str(worker_file),
        ]
        for route in routes:
            argv += ["--route", route]
        return argv

    def test_without_routes_the_workflow_file_is_used(self, tmp_path):
        ctx = role_scheduler.build_context(role_scheduler.parse_args(self._args(tmp_path)))
        assert ctx.routing.resolve("coder") == "refactorer"

    def test_routes_replace_the_file_entirely(self, tmp_path):
        ctx = role_scheduler.build_context(
            role_scheduler.parse_args(self._args(tmp_path, "coder=architect"))
        )
        assert ctx.routing.resolve("coder") == "architect"

    def test_a_role_only_in_the_file_is_gone_once_routes_are_given(self, tmp_path):
        # Replacement, not overlay: the file's `coder -> refactorer` must not survive
        # alongside a profile that never launches a refactorer.
        ctx = role_scheduler.build_context(
            role_scheduler.parse_args(self._args(tmp_path, "architect=human-in-the-loop"))
        )
        assert ctx.routing.resolve("coder") is None

    def test_a_conditional_route_survives_the_command_line(self, tmp_path):
        ctx = role_scheduler.build_context(
            role_scheduler.parse_args(
                self._args(tmp_path, "coder=architect", "coder=human-in-the-loop:architect")
            )
        )
        assert ctx.routing.resolve("coder") == "architect"
        assert ctx.routing.resolve("coder", "architect") == "human-in-the-loop"
