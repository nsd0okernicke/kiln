"""
Regression: a long-running scheduler must survive a bad cycle and leave evidence when it
does not.

Found live — a scheduler started, then vanished with no log file and no traceback anywhere,
because `main()` had no exception handling around `run_once`. An unattended swarm that dies
silently is worse than one that retries noisily.
"""

from __future__ import annotations

import logging

import pytest
from launcher import workspace
from launcher.config import parse_profile
from launcher.paths import KilnPaths
from scheduler import role_scheduler
from scheduler.role_scheduler import CycleResult

pytestmark = pytest.mark.integration


def _args(tmp_path, **overrides):
    worker = tmp_path / "coder-worker.md"
    worker.write_text("---\nname: coder-worker\n---\n\nbody\n", encoding="utf-8")
    workflow = tmp_path / "workflow.md"
    workflow.write_text("| coder | refactorer |\n", encoding="utf-8")
    argv = [
        "--role", "coder",
        "--branch", "main",
        "--db-path", str(tmp_path / "messages.db"),
        "--worktree", str(tmp_path),
        "--workflow", str(workflow),
        "--worker-agent", str(worker),
        "--poll-interval", "0.01",
    ]
    for key, value in overrides.items():
        argv += [f"--{key}", str(value)]
    return argv


@pytest.fixture
def stub_context(monkeypatch, tmp_path):
    from scheduler.routing import parse_routing_table
    from scheduler.worker_prompt import WorkerDefinition

    def _build(args):
        return role_scheduler.SchedulerContext(
            role="coder",
            branch="main",
            db_path=tmp_path / "messages.db",
            worktree=tmp_path,
            routing=parse_routing_table("| coder | refactorer |"),
            definition=WorkerDefinition(name="coder-worker", description="d", prompt="b"),
            run_worker=lambda **_kw: None,
        )

    monkeypatch.setattr(role_scheduler, "build_context", _build)


class TestLoopSurvivesFailures:
    def test_a_transient_failure_does_not_end_the_role(self, tmp_path, stub_context, monkeypatch):
        outcomes = [RuntimeError("database is locked"), CycleResult(role_scheduler.IDLE)]

        def flaky(ctx, state):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            state.halted = True  # end the test loop on the recovered cycle
            return item

        monkeypatch.setattr(role_scheduler, "run_once", flaky)
        monkeypatch.setattr(role_scheduler.time, "sleep", lambda _s: None)

        role_scheduler.main(_args(tmp_path))

        assert outcomes == [], "the loop must have continued past the raised error"

    def test_gives_up_after_repeated_failures(self, tmp_path, stub_context, monkeypatch):
        calls = {"n": 0}

        def always_fails(ctx, state):
            calls["n"] += 1
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(role_scheduler, "run_once", always_fails)
        monkeypatch.setattr(role_scheduler.time, "sleep", lambda _s: None)

        assert role_scheduler.main(_args(tmp_path)) == 1
        assert calls["n"] == role_scheduler.MAX_CONSECUTIVE_ERRORS

    def test_a_success_resets_the_failure_count(self, tmp_path, stub_context, monkeypatch):
        # Otherwise occasional unrelated hiccups would eventually add up to a shutdown.
        script = [RuntimeError("x"), CycleResult(role_scheduler.IDLE), RuntimeError("x")]
        seen = {"n": 0}

        def mixed(ctx, state):
            seen["n"] += 1
            if not script:
                state.halted = True
                return CycleResult(role_scheduler.IDLE)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(role_scheduler, "run_once", mixed)
        monkeypatch.setattr(role_scheduler.time, "sleep", lambda _s: None)

        role_scheduler.main(_args(tmp_path))

        assert seen["n"] == 4, "two isolated failures must not trip the limit"

    def test_the_failure_reason_survives_in_the_log_file(
        self, tmp_path, stub_context, monkeypatch
    ):
        # The point of the log file: the reason must outlive the pane.
        def boom(ctx, state):
            raise RuntimeError("the actual reason")

        monkeypatch.setattr(role_scheduler, "run_once", boom)
        monkeypatch.setattr(role_scheduler.time, "sleep", lambda _s: None)

        log_file = tmp_path / "logs" / "scheduler-coder.log"
        role_scheduler.main(_args(tmp_path, **{"log-file": log_file}))
        logging.shutdown()

        written = log_file.read_text(encoding="utf-8")
        assert "the actual reason" in written
        assert "Traceback" in written, "the stack trace must be preserved, not just the message"


class TestLogFile:
    def test_writes_a_log_file_when_asked(self, tmp_path):
        target = tmp_path / "logs" / "scheduler-coder.log"
        role_scheduler.configure_logging(target)
        logging.getLogger("scheduler.test").error("something went wrong")
        logging.shutdown()
        assert "something went wrong" in target.read_text(encoding="utf-8")

    def test_creates_the_log_directory(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "scheduler.log"
        role_scheduler.configure_logging(target)
        assert target.parent.is_dir()

    def test_works_without_a_log_file(self):
        role_scheduler.configure_logging(None)  # must not raise

    def test_launcher_passes_a_log_file(self, tmp_path):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig

        paths = KilnPaths.create(tmp_path / "p", tmp_path / "f")
        argv = build_agent_command(
            RoleConfig(role="coder", scheduler="python", mode="auto"), paths, "main"
        ).argv
        assert "--log-file" in argv
        assert argv[argv.index("--log-file") + 1].endswith("scheduler-coder.log")


class TestUntrackedKilnWarning:
    def test_warns_when_kiln_is_not_committed(self, tmp_path, caplog):
        # git worktree add only checks out tracked files, so worktrees would silently lack
        # the whole constitution.
        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        workspace.run_git(["init", "-b", "main"], paths.project_root, check=True)

        with caplog.at_level(logging.WARNING):
            warned = workspace.warn_if_kiln_untracked(paths)

        assert warned is True
        assert "not committed" in caplog.text
        assert "git add kiln/" in caplog.text

    def test_silent_when_kiln_is_tracked(self, tmp_path):
        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        (paths.constitution_dir / "workflow.md").write_text("x", encoding="utf-8")
        workspace.run_git(["init", "-b", "main"], paths.project_root, check=True)
        workspace.run_git(["add", "kiln/"], paths.project_root)

        assert workspace.warn_if_kiln_untracked(paths) is False

    def test_silent_for_an_unscaffolded_project(self, tmp_path):
        # A different error already covers this case; two messages would be noise.
        paths = KilnPaths.create(tmp_path, tmp_path)
        assert workspace.warn_if_kiln_untracked(paths) is False


def test_default_profile_still_parses():
    """The scheduler-all profile must stay valid as roles change."""
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / "kiln" / "framework" / "profiles.json").read_text("utf-8"))
    profile = parse_profile(config, "scheduler-all")
    scheduled = [r.role for r in profile.roles if r.uses_scheduler]
    interactive = [r.role for r in profile.roles if not r.uses_scheduler]
    assert scheduled == ["specifier", "coder", "refactorer", "architect"]
    assert interactive == ["human-in-the-loop"], "the human role must stay interactive"
