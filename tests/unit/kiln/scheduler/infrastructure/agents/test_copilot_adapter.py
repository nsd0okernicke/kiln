"""
Copilot one-shot adapter. Every flag asserted here was verified live against GitHub Copilot
CLI 1.0.78 during a spike; these tests pin those findings so a future edit cannot quietly
drop an isolation flag or misread the JSONL event shape.
"""

from __future__ import annotations

import io
import json
import subprocess
import time

import pytest

from kiln.scheduler.domain.worker_prompt import WorkerDefinition
from kiln.scheduler.infrastructure.agents import copilot_adapter

DEFINITION = WorkerDefinition(
    name="coder-worker",
    description="Does the coder work",
    prompt="# Coder Role\n\nImplement via TDD.",
)


def _message_event(content="", tool_requests=None):
    return {
        "type": "assistant.message",
        "data": {"content": content, "toolRequests": tool_requests or []},
    }


def _stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestBuildCommand:
    def _cmd(self, **overrides):
        args = {"agent_name": "coder-worker", "prompt": "do the thing"}
        args.update(overrides)
        return copilot_adapter.build_command(**args)

    def test_is_a_one_shot_prompt_invocation(self):
        command = self._cmd()
        assert command[0] == "copilot"
        assert command[command.index("-p") + 1] == "do the thing"

    def test_uses_json_output(self):
        command = self._cmd()
        assert command[command.index("--output-format") + 1] == "json"

    def test_allows_all_permissions(self):
        # Required for non-interactive scripting -- verified live, the CLI otherwise blocks
        # on a confirmation prompt with nobody able to answer it.
        assert "--allow-all" in self._cmd()

    def test_grants_explicit_allow_tool_rules_alongside_allow_all(self):
        # --allow-all's approveAllToolPermissionRequests can be silently zeroed out mid-session
        # by an enterprise managed-settings re-resolution (confirmed by decompiling the shipped
        # bundle), while an --allow-tool grant is stored under a separate `rules` key that
        # survives the flip -- this is the belt-and-suspenders fix for the "...and could not
        # request permission from user" failures seen on long scheduler-mode sessions.
        command = self._cmd()
        assert "--allow-tool=read" in command
        assert "--allow-tool=write" in command
        assert "--allow-tool=shell" in command

    def test_isolates_mcp_completely(self):
        # Verified live: workspace.prepare_agent_configs() writes kiln-db into Copilot's
        # *global* ~/.copilot/mcp-config.json whenever any role uses copilot, so an
        # unflagged worker would otherwise have live handoff-queue access.
        command = self._cmd()
        assert command[command.index("--disable-mcp-server") + 1] == "kiln-db"
        assert "--disable-builtin-mcps" in command

    def test_feeds_the_worker_definition_by_name(self):
        # No inline JSON payload the way Claude needs one -- Copilot reads the worker's
        # instructions straight off disk via --agent.
        command = self._cmd(agent_name="refactorer-worker")
        assert command[command.index("--agent") + 1] == "refactorer-worker"

    def test_model_is_optional(self):
        assert "--model" not in self._cmd()
        assert self._cmd(model="gpt-5.4")[self._cmd(model="gpt-5.4").index("--model") + 1] == (
            "gpt-5.4"
        )

    def test_log_dir_is_optional(self):
        # Off by default -- verified live it's substantial volume (200KB+ for a trivial
        # call), worth paying for only while actively diagnosing a failure.
        assert "--log-dir" not in self._cmd()
        command = self._cmd(log_dir="/tmp/agent-debug-coder-attempt1")
        assert command[command.index("--log-dir") + 1] == "/tmp/agent-debug-coder-attempt1"
        assert command[command.index("--log-level") + 1] == "all"


class TestParseCliOutput:
    def test_reads_the_final_message(self):
        stream = _stream(_message_event("KILN-STATUS: done built it"))
        envelope = copilot_adapter.parse_cli_output(stream)
        assert envelope["data"]["content"] == "KILN-STATUS: done built it"

    def test_last_non_empty_message_wins_over_a_tool_call_turn(self):
        # A turn that calls tools first emits an empty-content assistant.message (the tool
        # calls live in toolRequests instead) -- only the closing one carries the real reply.
        stream = _stream(
            _message_event("", tool_requests=[{"name": "create"}]),
            {"type": "tool.execution_start", "data": {"toolName": "create"}},
            {"type": "tool.execution_complete", "data": {"success": True}},
            _message_event("KILN-STATUS: done wrote the file"),
        )
        envelope = copilot_adapter.parse_cli_output(stream)
        assert envelope["data"]["content"] == "KILN-STATUS: done wrote the file"

    def test_skips_a_leading_notice_line(self):
        noisy = "Warning: something\n" + _stream(_message_event("done"))
        assert copilot_adapter.parse_cli_output(noisy)["data"]["content"] == "done"

    @pytest.mark.parametrize(
        "stdout",
        ["", "not json at all", "{broken json", json.dumps(_message_event(""))],
        ids=["empty", "garbage", "broken-json", "only-empty-message"],
    )
    def test_no_usable_message_raises(self, stdout):
        with pytest.raises(ValueError, match=r"no assistant\.message"):
            copilot_adapter.parse_cli_output(stdout)


class TestEventRendering:
    def test_tool_start_shows_the_tool_and_its_argument(self):
        event = {
            "type": "tool.execution_start",
            "data": {"toolName": "create", "arguments": {"path": "/p/ping.txt"}},
        }
        assert copilot_adapter.render_event(event) == [
            f"  {copilot_adapter.ICON_TOOL} create  /p/ping.txt"
        ]

    def test_tool_start_with_no_arguments_shows_the_bare_name(self):
        event = {"type": "tool.execution_start", "data": {"toolName": "list"}}
        assert copilot_adapter.render_event(event) == [f"  {copilot_adapter.ICON_TOOL} list"]

    def test_successful_tool_completion_stays_hidden(self):
        event = {"type": "tool.execution_complete", "data": {"success": True}}
        assert copilot_adapter.render_event(event) == []

    def test_failed_tool_completion_is_shown(self):
        event = {
            "type": "tool.execution_complete",
            "data": {"success": False, "result": {"content": "permission denied"}},
        }
        assert copilot_adapter.render_event(event) == [
            f"  {copilot_adapter.ICON_TOOL_ERROR} permission denied"
        ]

    def test_assistant_message_text_is_shown(self):
        event = _message_event("KILN-STATUS: done built it")
        assert copilot_adapter.render_event(event) == ["    KILN-STATUS: done built it"]

    def test_empty_assistant_message_renders_nothing(self):
        assert copilot_adapter.render_event(_message_event("")) == []

    def test_result_event_announces_completion(self):
        assert copilot_adapter.render_event({"type": "result"}) == [
            f"{copilot_adapter.ICON_FINISHED} worker finished"
        ]

    def test_bookkeeping_events_render_nothing(self):
        assert copilot_adapter.render_event({"type": "session.skills_loaded"}) == []


class FakePopen:
    """Stands in for a streaming copilot process."""

    def __init__(self, stdout="", stderr="", hang=False, returncode=0):
        self._lines = stdout.splitlines(keepends=True)
        self.stdout = self if not hang else _NeverEnds()
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False
        #: `terminate_tree` needs a pid for the platform kill and a liveness check before it.
        #: Negative so that if the OS call ever escaped a test it could not match a real
        #: process — `subprocess.run` is stubbed below precisely so it never does.
        self.pid = -1

    def poll(self):
        return 0 if self.killed else None

    def __iter__(self):
        return iter(self._lines)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


class _NeverEnds:
    """A stdout that blocks forever, to exercise the watchdog."""

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(10)
        raise StopIteration


class TestRunWorker:
    @pytest.fixture
    def fake_run(self, monkeypatch):
        """Replace Popen, capturing how the adapter invoked it. shutil.which is stubbed to
        keep the resolved binary path deterministic regardless of whether copilot is
        actually installed on the machine running the tests."""
        calls = {}
        monkeypatch.setattr(copilot_adapter.shutil, "which", lambda _name: None)

        def _factory(stdout="", stderr="", exc=None, hang=False, returncode=0):
            def _popen(command, **kwargs):
                calls["command"] = command
                calls["kwargs"] = kwargs
                if exc:
                    raise exc
                process = FakePopen(stdout=stdout, stderr=stderr, hang=hang, returncode=returncode)
                calls["process"] = process
                return process

            monkeypatch.setattr(copilot_adapter.subprocess, "Popen", _popen)
            # `terminate_tree` shells out to taskkill (Windows) or signals a process group
            # (POSIX); neither belongs in a unit test holding a fake process.
            monkeypatch.setattr(
                copilot_adapter.subprocess,
                "run",
                lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
            )
            return calls

        return _factory

    def test_streams_each_line_to_the_pane(self, fake_run, tmp_path):
        fake_run(
            stdout=_stream(
                {"type": "tool.execution_start", "data": {"toolName": "create"}},
                _message_event("KILN-STATUS: done built it"),
            )
        )
        seen = []
        copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=seen.append,
        )
        assert f"  {copilot_adapter.ICON_TOOL} create" in seen
        assert "    KILN-STATUS: done built it" in seen

    def test_timeout_kills_the_process(self, fake_run, tmp_path):
        calls = fake_run(hang=True)
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            timeout=1,
            on_output=lambda _line: None,
        )
        assert invocation.timed_out is True
        assert invocation.is_done is False
        assert calls["process"].killed is True

    def test_parses_a_successful_worker(self, fake_run, tmp_path):
        fake_run(stdout=_stream(_message_event("KILN-STATUS: done implemented feature")))
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done
        assert invocation.result.summary == "implemented feature"

    def test_debug_base_creates_the_log_dir_and_is_passed_through(self, fake_run, tmp_path):
        calls = fake_run(stdout=_stream(_message_event("KILN-STATUS: done x")))
        debug_base = tmp_path / "logs" / "agent-debug-coder-attempt1"
        copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
            debug_base=debug_base,
        )
        assert debug_base.is_dir()
        command = calls["command"]
        assert command[command.index("--log-dir") + 1] == str(debug_base)

    def test_redirects_stdin_and_separates_streams(self, fake_run, tmp_path):
        calls = fake_run(stdout=_stream(_message_event("KILN-STATUS: done x")))
        copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert calls["kwargs"]["stdin"] is subprocess.DEVNULL
        assert calls["kwargs"]["stdout"] is subprocess.PIPE
        assert calls["kwargs"]["stderr"] is subprocess.PIPE

    def test_blocked_worker_is_reported_not_raised(self, fake_run, tmp_path):
        fake_run(stdout=_stream(_message_event("KILN-STATUS: blocked missing fixtures")))
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.summary == "missing fixtures"

    def test_missing_binary_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(exc=OSError("copilot not found"))
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True

    def test_nonzero_exit_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(
            stdout=_stream(_message_event("KILN-STATUS: done x")),
            stderr="crashed",
            returncode=1,
        )
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True
        assert "crashed" in invocation.result.summary

    def test_unparseable_output_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stdout="total garbage", stderr="something broke")
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert "something broke" in invocation.result.summary

    def test_a_session_that_ran_but_never_closed_names_what_happened(self, fake_run, tmp_path):
        # Observed live: a session ran 8+ minutes, spent real credits, made zero changes, and
        # exited 0 with no non-empty assistant.message -- copilot's own end-of-session summary
        # (Changes/AI Credits/Tokens/Resume) lands on stderr. The raw stat block with no framing
        # reads as a crash; this must say plainly that the session ran and gave up.
        events = _stream(
            {"type": "tool.execution_start", "data": {"toolName": "grep"}},
            _message_event(""),
        )
        fake_run(stdout=events, stderr="Changes    +0 -0\nAI Credits 136 (8m 22s)")
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert "session ended with no final reply" in invocation.result.summary
        assert "2 stream events seen" in invocation.result.summary
        assert "AI Credits" in invocation.result.summary

    def test_missing_sentinel_is_blocked(self, fake_run, tmp_path):
        fake_run(stdout=_stream(_message_event("I finished but forgot the sentinel.")))
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.sentinel_found is False

    def test_cost_is_not_tracked_for_this_backend(self, fake_run, tmp_path):
        # No dollar figure exists anywhere in Copilot's output -- confirmed live.
        fake_run(stdout=_stream(_message_event("KILN-STATUS: done x")))
        invocation = copilot_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.cost_usd == 0.0
