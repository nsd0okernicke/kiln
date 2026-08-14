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


class TestStaleMessageRecovery:
    """
    Work left `processing` by a killed scheduler is silently lost today: never re-served,
    never counted, never surfaced. Recovery happens once at startup, because that is the
    moment the role's own queue is provably unattended.
    """

    def _stranded(self, tmp_path, **kwargs):
        from scheduler import db

        db.ensure_schema(tmp_path / "messages.db")
        return db.insert_handoff(
            tmp_path / "messages.db", kwargs.get("sender", "specifier"),
            kwargs.get("target", "coder"), "payload", "main",
            work_item=kwargs.get("work_item"),
        )

    def test_startup_re_serves_a_message_left_mid_cycle(
        self, tmp_path, stub_context, monkeypatch, caplog
    ):
        from scheduler import db

        message_id = self._stranded(tmp_path)
        db.mark_processing(tmp_path / "messages.db", message_id)

        def one_cycle(ctx, state):
            state.halted = True
            return CycleResult(role_scheduler.IDLE)

        monkeypatch.setattr(role_scheduler, "run_once", one_cycle)
        with caplog.at_level(logging.WARNING):
            role_scheduler.main(_args(tmp_path))

        served = db.fetch_and_deliver(tmp_path / "messages.db", "coder", "main")
        assert served is not None and served["id"] == message_id

    def test_each_recovered_message_is_logged(self, tmp_path, stub_context, caplog):
        from scheduler import db

        message_id = self._stranded(tmp_path, work_item="add-login")
        db.mark_processing(tmp_path / "messages.db", message_id)
        ctx = _context(tmp_path)

        with caplog.at_level(logging.WARNING):
            assert role_scheduler.recover_stale_messages(ctx) == 1

        assert message_id[:8] in caplog.text
        assert "add-login" in caplog.text

    def test_the_replay_hazard_is_warned_about(self, tmp_path, stub_context, caplog):
        # The cycle is replayed against a worktree that may still hold partial work, so the
        # role can redo work it already did. An operator seeing that needs it explained.
        from scheduler import db

        db.mark_processing(tmp_path / "messages.db", self._stranded(tmp_path))

        with caplog.at_level(logging.WARNING):
            role_scheduler.recover_stale_messages(_context(tmp_path))

        assert "redo work it already did" in caplog.text

    def test_a_clean_start_says_nothing(self, tmp_path, stub_context, caplog):
        # Recovery is an exception report. A warning on every ordinary start would train
        # operators to ignore the one that matters.
        self._stranded(tmp_path)
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            assert role_scheduler.recover_stale_messages(_context(tmp_path)) == 0

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_an_unusable_queue_does_not_kill_the_role_at_startup(self, tmp_path, caplog):
        # Recovery is a repair on the way in, not a precondition. An unreadable queue -- no
        # table yet, or the pre-work_item schema ensure_schema does not migrate -- has to
        # surface through the poll loop, which reports it with a traceback and exits
        # cleanly. Raising here would kill the role before it reached that machinery.
        with caplog.at_level(logging.WARNING):
            assert role_scheduler.recover_stale_messages(_context(tmp_path)) == 0

        assert "could not check for messages left mid-cycle" in caplog.text


def _context(tmp_path):
    from scheduler.routing import parse_routing_table
    from scheduler.worker_prompt import WorkerDefinition

    return role_scheduler.SchedulerContext(
        role="coder",
        branch="main",
        db_path=tmp_path / "messages.db",
        worktree=tmp_path,
        routing=parse_routing_table("| coder | refactorer |"),
        definition=WorkerDefinition(name="coder-worker", description="d", prompt="b"),
        run_worker=lambda **_kw: None,
    )


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


class TestShippedRoutingTable:
    """
    The framework's own default profile must describe a cycle that can *terminate*.

    Found live: the shipped table routed every specifier handoff to `coder`, and the one
    exception — an architect's completed-cycle report goes back to the human, not around
    again — lived only as a prose note underneath the table. The wrapper LLM could read
    that note; the scheduler reads the table. So a finished cycle fed straight back into
    coder -> refactorer -> architect -> specifier -> coder, forever, and the human was
    never told the work was done.
    """

    @pytest.fixture
    def table(self):
        # Routing moved out of workflow.md and into the profile: the file is injected
        # verbatim into wrapper-mode instructions, so a table written there and a profile
        # that declares its own were two sources that could disagree. The file now carries
        # a {{ROUTING_TABLE}} placeholder rendered from whichever profile is running, and
        # the shipped default profile is the thing this class is really about.
        return _shipped_profile().routing

    def test_an_architect_report_returns_to_the_human(self, table):
        assert table.resolve("specifier", "architect") == "human-in-the-loop"

    def test_a_new_request_still_reaches_the_coder(self, table):
        # The conditional row must not shadow the specifier's default route.
        assert table.resolve("specifier", "human-in-the-loop") == "coder"

    def test_every_role_has_a_route(self, table):
        for role in ("human-in-the-loop", "specifier", "coder", "refactorer", "architect"):
            assert table.resolve(role) is not None, f"{role} would escalate every handoff"

    def test_the_cycle_comes_back_to_the_human(self, table):
        """Walk the graph the way the scheduler does. Not reaching the human is the bug."""
        role, sender = "human-in-the-loop", None
        visited = []
        for _ in range(12):
            target = table.resolve(role, sender)
            assert target, f"{role} has no route"
            visited.append(target)
            if target == "human-in-the-loop":
                break
            role, sender = target, role
        else:
            pytest.fail(f"the cycle never returns to the human: {' -> '.join(visited)}")

        assert visited == [
            "specifier", "coder", "refactorer", "architect", "specifier", "human-in-the-loop"
        ]


def _shipped_profile(name: str | None = None):
    """
    The shipped profile of that name, or whichever one is currently the default.

    Follows the top-level `default` key rather than hardcoding a profile name, so renaming
    the default (`default` -> `full` when profiles became workflow-shaped) does not break
    every test that just wanted "the one a plain `kiln` launches".
    """
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / "kiln" / "framework" / "profiles.json").read_text("utf-8"))
    return parse_profile(config, name or config["default"])


def test_default_profile_still_parses():
    """The default profile must stay valid as roles change."""
    profile = _shipped_profile()
    scheduled = [r.role for r in profile.roles if r.uses_scheduler]
    inboxes = [r.role for r in profile.roles if r.is_inbox]
    dashboards = [r.role for r in profile.roles if r.is_dashboard]
    interactive = [r.role for r in profile.roles if not r.uses_scheduler and not r.is_passive]
    assert scheduled == ["specifier", "coder", "refactorer", "architect"]
    assert interactive == ["human-in-the-loop"], "the human role must stay interactive"
    assert inboxes == ["inbox"], "escalations need somewhere visible to land"
    assert dashboards == ["dashboard"], "the swarm-wide view needs somewhere to live"


class TestShippedInboxPane:
    def test_it_watches_the_human_queue_not_its_own(self):
        # An inbox watching 'inbox' would show an empty queue forever, which looks exactly
        # like a working one.
        assert _shipped_profile().role("inbox").watched_role == "human-in-the-loop"

    def test_it_shares_the_first_tab_with_the_human(self):
        # The whole point of the change: no second terminal to open by hand.
        tab = _shipped_profile().layout["tabs"][0]
        assert [pane["role"] for pane in tab["panes"]] == ["human-in-the-loop", "inbox"]

    def test_it_is_a_small_strip_beneath_the_session(self):
        pane = _shipped_profile().layout["tabs"][0]["panes"][1]
        assert pane["direction"] == "Bottom"
        assert 0 < pane["size"] < 0.5, "the human's session must keep most of the tab"

    def test_the_human_role_still_owns_the_root_mcp_config(self):
        # Both live in the project root; the inbox must not win current_dir_role.
        assert _shipped_profile().current_dir_role.role == "human-in-the-loop"


class TestShippedDashboardPane:
    def test_it_has_its_own_dedicated_tab(self):
        # Not a strip like the inbox pairing -- a full tab, per its own purpose (a swarm-wide
        # view, not a companion notification for one role).
        tabs = _shipped_profile().layout["tabs"]
        dashboard_tabs = [t for t in tabs if any(p["role"] == "dashboard" for p in t["panes"])]
        assert len(dashboard_tabs) == 1
        assert len(dashboard_tabs[0]["panes"]) == 1

    def test_it_does_not_steal_the_root_mcp_config_from_the_human(self):
        # Also "@current", same as human-in-the-loop and inbox -- current_dir_role must
        # still resolve to the human, not whichever passive pane happens to be listed first.
        assert _shipped_profile().current_dir_role.role == "human-in-the-loop"
