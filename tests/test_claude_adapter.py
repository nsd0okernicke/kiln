"""
Claude one-shot adapter. Every flag asserted here was verified live against Claude Code
2.1.224 during the spike; these tests pin those findings so a future edit cannot quietly
drop an isolation flag.
"""

from __future__ import annotations

import io
import json
import subprocess
import time

import pytest
from scheduler.adapters import claude_adapter
from scheduler.worker_prompt import WorkerDefinition

DEFINITION = WorkerDefinition(
    name="coder-worker",
    description="Does the coder work",
    prompt="# Coder Role\n\nImplement via TDD.",
    model="claude-sonnet-5",
)


def _envelope(**overrides):
    """A stream containing just the final `result` event."""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Did the work.\nKILN-STATUS: done implemented feature",
        "total_cost_usd": 0.0321,
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
            "model": "sonnet",
        }
        args.update(overrides)
        return claude_adapter.build_command(**args)

    def test_is_a_one_shot_print_invocation(self):
        command = self._cmd()
        assert command[0] == "claude"
        assert "-p" in command

    def test_streams_output_rather_than_waiting_for_one_blob(self):
        # Plain `json` emits nothing until the worker finishes, leaving the pane looking
        # hung — the same invisibility problem that motivated replacing the wrapper.
        command = self._cmd()
        assert command[command.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in command, "streaming in print mode requires --verbose"

    def test_isolates_mcp_completely(self):
        # Verified live: --strict-mcp-config with no --mcp-config yields zero MCP tools,
        # which is what stops a worker sending its own handoffs.
        command = self._cmd()
        assert "--strict-mcp-config" in command
        assert "--mcp-config" not in command

    def test_scopes_settings_to_the_project(self):
        # Keeps project skills, drops the operator's user-global plugin skills.
        command = self._cmd()
        assert command[command.index("--setting-sources") + 1] == "project"

    def test_never_uses_bare_mode(self):
        # --bare suppresses CLAUDE.md but breaks OAuth entirely (verified: "Not logged in").
        assert "--bare" not in self._cmd()

    def test_model_is_always_explicit(self):
        # The CLI default is Opus: 5-10x the cost of Sonnet on an identical call.
        assert self._cmd(model="sonnet")[self._cmd().index("--model") + 1] == "sonnet"

    def test_feeds_the_worker_definition(self):
        command = self._cmd(agents_json='{"x": 1}', agent_name="x")
        assert command[command.index("--agents") + 1] == '{"x": 1}'
        assert command[command.index("--agent") + 1] == "x"

    def test_prompt_is_the_final_positional_argument(self):
        assert self._cmd(prompt="the task")[-1] == "the task"

    def test_permission_mode_is_passed(self):
        assert self._cmd()[self._cmd().index("--permission-mode") + 1] == "bypassPermissions"

    def test_budget_cap_is_optional(self):
        assert "--max-budget-usd" not in self._cmd()
        assert "--max-budget-usd" in self._cmd(max_budget_usd=1.5)

    def test_debug_file_is_optional(self):
        # Off by default -- verified live it's substantial volume (191 lines for a trivial
        # call), worth paying for only while actively diagnosing a failure.
        assert "--debug-file" not in self._cmd()
        command = self._cmd(debug_log="/tmp/agent-debug-coder-attempt1.log")
        assert command[command.index("--debug-file") + 1] == "/tmp/agent-debug-coder-attempt1.log"


class TestParseCliOutput:
    def test_reads_a_plain_envelope(self):
        assert claude_adapter.parse_cli_output(_envelope())["is_error"] is False

    def test_skips_a_leading_notice_line(self):
        # The CLI emits warnings ahead of the envelope in some conditions.
        noisy = "Warning: no stdin data received in 3s, proceeding without it.\n" + _envelope()
        assert claude_adapter.parse_cli_output(noisy)["num_turns"] == 1

    @pytest.mark.parametrize("stdout", ["", "not json at all", "{broken json"])
    def test_unparseable_output_raises(self, stdout):
        with pytest.raises(ValueError, match="no JSON envelope"):
            claude_adapter.parse_cli_output(stdout)


class TestEventRendering:
    """What an operator watching the pane actually sees."""

    def test_init_event_announces_the_session(self):
        rendered = claude_adapter.render_event({"type": "system", "subtype": "init"})
        assert rendered == [f"{claude_adapter.ICON_SESSION} worker session started"]

    def test_assistant_text_is_shown(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Reading the spec"}]},
        }
        assert claude_adapter.render_event(event) == ["    Reading the spec"]

    def test_multiline_text_is_indented_per_line(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "one\ntwo"}]},
        }
        assert claude_adapter.render_event(event) == ["    one", "    two"]

    def test_tool_calls_are_surfaced(self):
        # Seeing which tools the worker reaches for is most of the value of watching.
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Edit"}]},
        }
        assert claude_adapter.render_event(event) == [f"  {claude_adapter.ICON_TOOL} Edit"]

    def test_tool_calls_show_what_they_operate_on(self):
        # A pane full of bare '[icon] Bash' lines tells an operator nothing about what the
        # worker is doing — the whole reason for watching a scheduler pane.
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}
                ]
            },
        }
        assert claude_adapter.render_event(event) == [
            f"  {claude_adapter.ICON_TOOL} Bash  pytest -q"
        ]


class TestToolSummaries:
    @pytest.mark.parametrize(
        ("name", "payload", "expected"),
        [
            ("Bash", {"command": "ls -la"}, "Bash  ls -la"),
            ("Read", {"file_path": "/p/spec.md"}, "Read  /p/spec.md"),
            ("Write", {"file_path": "/p/out.md", "content": "x" * 500}, "Write  /p/out.md"),
            ("Grep", {"pattern": "TODO", "path": "/p"}, "Grep  TODO"),
            ("Skill", {"skill": "kiln-handoff"}, "Skill  kiln-handoff"),
            ("WebFetch", {"url": "https://example.com"}, "WebFetch  https://example.com"),
        ],
    )
    def test_each_tool_shows_its_most_useful_field(self, name, payload, expected):
        assert claude_adapter.summarise_tool_use(name, payload) == expected

    def test_todo_writes_stay_bare(self):
        # A serialised todo array is pure noise in a pane.
        assert claude_adapter.summarise_tool_use("TodoWrite", {"todos": [1, 2]}) == "TodoWrite"

    def test_unknown_tools_fall_back_to_a_recognisable_field(self):
        # The CLI adds tools over time; a new one must not silently lose its detail.
        summary = claude_adapter.summarise_tool_use("BrandNewTool", {"file_path": "/p/x"})
        assert summary == "BrandNewTool  /p/x"

    def test_a_tool_with_nothing_useful_renders_its_name_alone(self):
        assert claude_adapter.summarise_tool_use("Mystery", {"opaque": 1}) == "Mystery"

    def test_multiline_input_is_collapsed_to_one_line(self):
        # A heredoc would otherwise break the pane's one-line-per-event layout.
        summary = claude_adapter.summarise_tool_use("Bash", {"command": "cat <<EOF\na\nb\nEOF"})
        assert "\n" not in summary
        assert summary == "Bash  cat <<EOF a b EOF"

    def test_long_input_is_truncated(self):
        summary = claude_adapter.summarise_tool_use("Bash", {"command": "x" * 5000})
        assert len(summary) <= claude_adapter.MAX_DETAIL_CHARS + len("Bash  ")
        assert summary.endswith("\N{HORIZONTAL ELLIPSIS}")


class TestToolFailures:
    """A failing tool is usually *why* a worker ends up blocked; it must be visible."""

    def test_tool_errors_are_shown(self):
        event = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "command not found"}
                ]
            },
        }
        assert claude_adapter.render_event(event) == [
            f"  {claude_adapter.ICON_TOOL_ERROR} command not found"
        ]

    def test_successful_tool_results_stay_hidden(self):
        # Full results would bury the pane; only failures earn a line.
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok" * 5000}]},
        }
        assert claude_adapter.render_event(event) == []

    def test_structured_error_content_is_flattened(self):
        event = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": [{"type": "text", "text": "file not found"}],
                    }
                ]
            },
        }
        assert claude_adapter.render_event(event) == [
            f"  {claude_adapter.ICON_TOOL_ERROR} file not found"
        ]

    def test_an_error_with_no_content_still_reports_something(self):
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True}]},
        }
        assert claude_adapter.render_event(event) == [
            f"  {claude_adapter.ICON_TOOL_ERROR} tool failed"
        ]

    def test_result_event_reports_cost(self):
        rendered = claude_adapter.render_event({"type": "result", "total_cost_usd": 0.05})
        assert rendered == [f"{claude_adapter.ICON_FINISHED} worker finished (cost $0.0500)"]

    def test_empty_text_blocks_are_skipped(self):
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "  "}]}}
        assert claude_adapter.render_event(event) == []

    def test_bookkeeping_events_render_nothing(self):
        assert claude_adapter.render_event({"type": "rate_limit_event"}) == []


class TestParseStream:
    def test_finds_the_result_event_among_others(self):
        stream = _stream(
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": []}},
            json.loads(_envelope()),
        )
        assert claude_adapter.parse_cli_output(stream)["type"] == "result"

    def test_last_result_wins(self):
        stream = _stream(
            json.loads(_envelope(result="first")), json.loads(_envelope(result="second"))
        )
        assert claude_adapter.parse_cli_output(stream)["result"] == "second"

    def test_falls_back_to_the_last_object_without_a_result_event(self):
        stream = _stream({"type": "assistant", "message": {"content": []}})
        assert claude_adapter.parse_cli_output(stream)["type"] == "assistant"


class FakePopen:
    """Stands in for a streaming claude process."""

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
        """Replace Popen, capturing how the adapter invoked it."""
        calls = {}

        def _factory(stdout="", stderr="", exc=None, hang=False):
            def _popen(command, **kwargs):
                calls["command"] = command
                calls["kwargs"] = kwargs
                if exc:
                    raise exc
                process = FakePopen(stdout=stdout, stderr=stderr, hang=hang)
                calls["process"] = process
                return process

            monkeypatch.setattr(claude_adapter.subprocess, "Popen", _popen)
            # `terminate_tree` shells out to taskkill (Windows) or signals a process group
            # (POSIX); neither belongs in a unit test holding a fake process.
            monkeypatch.setattr(
                claude_adapter.subprocess, "run",
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
        claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=seen.append,
        )
        assert f"{claude_adapter.ICON_SESSION} worker session started" in seen
        assert "    working" in seen

    def test_timeout_kills_the_process(self, fake_run, tmp_path):
        calls = fake_run(hang=True)
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            timeout=1, on_output=lambda _line: None,
        )
        assert invocation.timed_out is True
        assert invocation.is_done is False
        assert calls["process"].killed is True

    def test_parses_a_successful_worker(self, fake_run, tmp_path):
        fake_run(stdout=_envelope())
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done
        assert invocation.result.summary == "implemented feature"
        assert invocation.cost_usd == pytest.approx(0.0321)

    def test_redirects_stdin_and_separates_streams(self, fake_run, tmp_path):
        # Without DEVNULL the CLI blocks ~3s per call; merging the streams would interleave
        # stderr into the event stream and corrupt parsing.
        calls = fake_run(stdout=_envelope())
        claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert calls["kwargs"]["stdin"] is subprocess.DEVNULL
        assert calls["kwargs"]["stdout"] is subprocess.PIPE
        assert calls["kwargs"]["stderr"] is subprocess.PIPE

    def test_blocked_worker_is_reported_not_raised(self, fake_run, tmp_path):
        fake_run(stdout=_envelope(result="KILN-STATUS: blocked missing fixtures"))
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.summary == "missing fixtures"

    def test_missing_binary_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(exc=OSError("claude not found"))
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True

    def test_cli_error_envelope_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stdout=_envelope(is_error=True, result="Not logged in"))
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True
        assert "Not logged in" in invocation.result.summary

    def test_unparseable_output_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stdout="total garbage", stderr="something broke")
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert "something broke" in invocation.result.summary

    def test_missing_sentinel_is_blocked(self, fake_run, tmp_path):
        fake_run(stdout=_envelope(result="I finished the work but forgot the sentinel."))
        invocation = claude_adapter.run_worker(
            definition=DEFINITION, prompt="p", cwd=tmp_path, model="sonnet",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.sentinel_found is False


class TestWorkerToolsAreHonoured:
    """
    The Claude adapter sends the worker's declared tool list; the grok adapter does not.

    Guards a saving measured live (30 tools -> 10 for the coder) and, more importantly, a
    silent failure: a malformed `tools` value makes Claude Code discard the whole agent
    definition rather than complain, so the worker would lose its persona entirely.
    """

    RESTRICTED = WorkerDefinition(
        name="coder-worker",
        description="Does the coder work",
        prompt="# Coder Role",
        tools="Read, Grep",
    )

    def test_claude_sends_the_declared_tools_as_an_array(self):
        payload = json.loads(
            claude_adapter.build_agents_payload(self.RESTRICTED, include_tools=True)
        )
        assert payload["coder-worker"]["tools"] == ["Read", "Grep"]

    def test_grok_does_not_until_its_cli_is_spiked(self):
        # Same payload builder, unverified CLI: a wrong shape there would silently strip
        # the persona rather than error, which is not worth a size saving.
        from scheduler.adapters import grok_adapter

        payload = json.loads(grok_adapter.build_agents_payload(self.RESTRICTED))
        assert "tools" not in payload["coder-worker"]
