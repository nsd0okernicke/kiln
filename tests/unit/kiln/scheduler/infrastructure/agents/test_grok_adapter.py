"""
Grok one-shot adapter. Every flag asserted here was verified live against `grok` 1.0.0
(3cd0d0cbce, stable) during a spike; these tests pin those findings so a future edit cannot
quietly drop an isolation flag or misread the JSONL event shape.

The wire format (`--output-format streaming-messages-json`) is documented by the CLI itself as
Anthropic Messages API-compatible, and verified live to match what claude_adapter.py already
parses -- so this test file mirrors tests/test_claude_adapter.py closely, differing mainly in
grok's own flags and lowercase/snake_case tool names.
"""

from __future__ import annotations

import io
import json
import subprocess
import time

import pytest

from kiln.scheduler.domain.worker_prompt import WorkerDefinition
from kiln.scheduler.infrastructure.agents import grok_adapter

DEFINITION = WorkerDefinition(
    name="coder-worker",
    description="Does the coder work",
    prompt="# Coder Role\n\nImplement via TDD.",
)


def _envelope(**overrides):
    """A stream containing just the final `result` event."""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Did the work.\nKILN-STATUS: done implemented feature",
        "total_cost_usd": 0.02508,
        "num_turns": 1,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestBuildCommand:
    def _cmd(self, **overrides):
        args = {
            "agents_json": '{"coder-worker": {}}',
            "agent_name": "coder-worker",
            "prompt": "do the thing",
        }
        args.update(overrides)
        return grok_adapter.build_command(**args)

    def test_is_a_one_shot_prompt_invocation(self):
        command = self._cmd()
        assert command[0] == "grok"
        assert command[command.index("-p") + 1] == "do the thing"

    def test_streams_anthropic_compatible_json(self):
        command = self._cmd()
        assert command[command.index("--output-format") + 1] == "streaming-messages-json"

    def test_auto_approves_for_non_interactive_use(self):
        assert "--always-approve" in self._cmd()

    def test_disables_recursive_subagent_spawning(self):
        # The worker-isolation equivalent of Claude having no Agent tool, and Codex's
        # mcp_servers = {} -- verified live, this removes spawn_subagent from the tool list.
        assert "--no-subagents" in self._cmd()

    def test_feeds_the_worker_definition(self):
        command = self._cmd(agents_json='{"x": 1}', agent_name="x")
        assert command[command.index("--agents") + 1] == '{"x": 1}'
        assert command[command.index("--agent") + 1] == "x"

    def test_model_is_optional(self):
        assert "-m" not in self._cmd()
        assert self._cmd(model="grok-4.5")[self._cmd(model="grok-4.5").index("-m") + 1] == (
            "grok-4.5"
        )


class TestParseCliOutput:
    def test_reads_a_plain_envelope(self):
        assert grok_adapter.parse_cli_output(_envelope())["is_error"] is False

    def test_skips_a_leading_notice_line(self):
        noisy = "Warning: no stdin data received in 3s, proceeding without it.\n" + _envelope()
        assert grok_adapter.parse_cli_output(noisy)["num_turns"] == 1

    def test_last_result_wins(self):
        stream = _stream(
            json.loads(_envelope(result="first")), json.loads(_envelope(result="second"))
        )
        assert grok_adapter.parse_cli_output(stream)["result"] == "second"

    def test_falls_back_to_the_last_object_without_a_result_event(self):
        stream = _stream({"type": "assistant", "message": {"content": []}})
        assert grok_adapter.parse_cli_output(stream)["type"] == "assistant"

    @pytest.mark.parametrize("stdout", ["", "not json at all", "{broken json"])
    def test_unparseable_output_raises(self, stdout):
        with pytest.raises(ValueError, match="no JSON envelope"):
            grok_adapter.parse_cli_output(stdout)


class TestEventRendering:
    def test_init_event_announces_the_session(self):
        rendered = grok_adapter.render_event({"type": "system", "subtype": "init"})
        assert rendered == [f"{grok_adapter.ICON_SESSION} worker session started"]

    def test_assistant_text_is_shown(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Reading the spec"}]},
        }
        assert grok_adapter.render_event(event) == ["    Reading the spec"]

    def test_tool_calls_are_surfaced(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "run_terminal_command",
                        "input": {"command": "pytest -q"},
                    }
                ]
            },
        }
        assert grok_adapter.render_event(event) == [
            f"  {grok_adapter.ICON_TOOL} run_terminal_command  pytest -q"
        ]

    def test_write_tool_shows_the_file_path(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "write", "input": {"file_path": "/p/x.py"}}
                ]
            },
        }
        assert grok_adapter.render_event(event) == [f"  {grok_adapter.ICON_TOOL} write  /p/x.py"]

    def test_result_event_reports_cost(self):
        rendered = grok_adapter.render_event({"type": "result", "total_cost_usd": 0.05})
        assert rendered == [f"{grok_adapter.ICON_FINISHED} worker finished (cost $0.0500)"]

    def test_bookkeeping_events_render_nothing(self):
        assert grok_adapter.render_event({"type": "thread.started"}) == []


class TestToolSummaries:
    @pytest.mark.parametrize(
        ("name", "payload", "expected"),
        [
            ("run_terminal_command", {"command": "ls -la"}, "run_terminal_command  ls -la"),
            ("read_file", {"file_path": "/p/spec.md"}, "read_file  /p/spec.md"),
            ("grep", {"pattern": "TODO", "path": "/p"}, "grep  TODO"),
            ("web_search", {"query": "xai api"}, "web_search  xai api"),
            ("spawn_subagent", {"description": "run tests"}, "spawn_subagent  run tests"),
        ],
    )
    def test_each_tool_shows_its_most_useful_field(self, name, payload, expected):
        assert grok_adapter.summarise_tool_use(name, payload) == expected

    def test_todo_write_stays_bare(self):
        assert grok_adapter.summarise_tool_use("todo_write", {"todos": [1, 2]}) == "todo_write"

    def test_unknown_tools_fall_back_to_a_recognisable_field(self):
        summary = grok_adapter.summarise_tool_use("brand_new_tool", {"file_path": "/p/x"})
        assert summary == "brand_new_tool  /p/x"

    def test_a_tool_with_nothing_useful_renders_its_name_alone(self):
        assert grok_adapter.summarise_tool_use("mystery", {"opaque": 1}) == "mystery"


class TestToolFailures:
    def test_tool_errors_are_shown(self):
        event = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "command not found"}
                ]
            },
        }
        assert grok_adapter.render_event(event) == [
            f"  {grok_adapter.ICON_TOOL_ERROR} command not found"
        ]

    def test_successful_tool_results_stay_hidden(self):
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok" * 5000}]},
        }
        assert grok_adapter.render_event(event) == []


class FakePopen:
    """Stands in for a streaming grok process."""

    def __init__(self, stdout="", stderr="", hang=False):
        self._lines = stdout.splitlines(keepends=True)
        self.stdout = self if not hang else _NeverEnds()
        self.stderr = io.StringIO(stderr)
        self.returncode = 0
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
        return 0

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
        keep the resolved binary path deterministic regardless of whether grok is actually
        installed on the machine running the tests."""
        calls = {}
        monkeypatch.setattr(grok_adapter.shutil, "which", lambda _name: None)

        def _factory(stdout="", stderr="", exc=None, hang=False):
            def _popen(command, **kwargs):
                calls["command"] = command
                calls["kwargs"] = kwargs
                if exc:
                    raise exc
                process = FakePopen(stdout=stdout, stderr=stderr, hang=hang)
                calls["process"] = process
                return process

            monkeypatch.setattr(grok_adapter.subprocess, "Popen", _popen)
            # `terminate_tree` shells out to taskkill (Windows) or signals a process group
            # (POSIX); neither belongs in a unit test holding a fake process.
            monkeypatch.setattr(
                grok_adapter.subprocess,
                "run",
                lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
            )
            return calls

        return _factory

    def test_streams_each_line_to_the_pane(self, fake_run, tmp_path):
        fake_run(
            stdout=_stream(
                {"type": "system", "subtype": "init"},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "working"}]},
                },
                json.loads(_envelope()),
            )
        )
        seen = []
        grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=seen.append,
        )
        assert f"{grok_adapter.ICON_SESSION} worker session started" in seen
        assert "    working" in seen

    def test_timeout_kills_the_process(self, fake_run, tmp_path):
        calls = fake_run(hang=True)
        invocation = grok_adapter.run_worker(
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
        fake_run(stdout=_envelope())
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done
        assert invocation.result.summary == "implemented feature"

    def test_cost_is_genuinely_tracked_for_this_backend(self, fake_run, tmp_path):
        # Unlike Copilot/Codex, grok reports a real total_cost_usd -- confirmed live.
        fake_run(stdout=_envelope(total_cost_usd=0.02508))
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.cost_usd == pytest.approx(0.02508)

    def test_redirects_stdin_and_separates_streams(self, fake_run, tmp_path):
        calls = fake_run(stdout=_envelope())
        grok_adapter.run_worker(
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
        fake_run(stdout=_envelope(result="KILN-STATUS: blocked missing fixtures"))
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.summary == "missing fixtures"

    def test_missing_binary_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(exc=OSError("grok not found"))
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True

    def test_cli_error_envelope_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stdout=_envelope(is_error=True, result="Not logged in"))
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True
        assert "Not logged in" in invocation.result.summary

    def test_unparseable_output_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stdout="total garbage", stderr="something broke")
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert "something broke" in invocation.result.summary

    def test_missing_sentinel_is_blocked(self, fake_run, tmp_path):
        fake_run(stdout=_envelope(result="I finished the work but forgot the sentinel."))
        invocation = grok_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.sentinel_found is False
