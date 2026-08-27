"""
Codex one-shot adapter. Every flag asserted here was verified live against `codex-cli`
0.147.0 during a spike; these tests pin those findings so a future edit cannot quietly drop
an isolation flag or misread the JSONL event shape.
"""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import pytest

from kiln.scheduler.domain.worker_prompt import WorkerDefinition
from kiln.scheduler.infrastructure.agents import codex_adapter

DEFINITION = WorkerDefinition(
    name="refactorer-worker",
    description="Does the refactorer work",
    prompt="# Refactorer Role\n\nRun quality gates.",
)


def _stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestBuildFullPrompt:
    def test_persona_precedes_the_task(self):
        # There is no --agent flag for `codex exec` -- the worker's persona has to be
        # embedded directly ahead of the task prompt.
        prompt = codex_adapter.build_full_prompt(DEFINITION, "do the task")
        assert prompt.index("# Refactorer Role") < prompt.index("do the task")


class TestBuildCommand:
    def _cmd(self, **overrides):
        args = {"prompt": "do the thing", "output_file": "/tmp/out.txt"}
        args.update(overrides)
        return codex_adapter.build_command(**args)

    def test_is_a_one_shot_exec_invocation(self):
        command = self._cmd()
        assert command[0] == "codex"
        assert command[1] == "exec"
        assert command[2] == "do the thing"

    def test_uses_json_streaming(self):
        assert "--json" in self._cmd()

    def test_bypasses_approvals_and_sandbox(self):
        # Matches the flag launcher/commands.py's wrapper-mode _codex_command already uses.
        assert "--dangerously-bypass-approvals-and-sandbox" in self._cmd()

    def test_ignores_the_per_role_wrapper_config(self):
        # --ignore-user-config: the CODEX_HOME/config.toml workspace.prepare_agent_configs()
        # writes is meant for the wrapper's interactive session, not this one-shot call.
        assert "--ignore-user-config" in self._cmd()

    def test_writes_the_final_message_to_the_given_file(self):
        command = self._cmd(output_file="/tmp/last.txt")
        assert command[command.index("-o") + 1] == "/tmp/last.txt"

    def test_model_is_optional(self):
        assert "-m" not in self._cmd()
        assert self._cmd(model="o3")[self._cmd(model="o3").index("-m") + 1] == "o3"

    def test_no_proxy_flags_unless_a_base_url_is_given(self):
        assert "-c" not in self._cmd()


class TestProxyConfigArgs:
    """
    Codex has no base-URL environment variable, so routing it needs `-c` overrides. The
    synthetic provider must explicitly opt into the role's copied ChatGPT authentication.
    """

    def test_nothing_without_a_url(self):
        assert codex_adapter.proxy_config_args(None) == []
        assert codex_adapter.proxy_config_args("") == []

    def test_selects_the_synthetic_provider(self):
        args = codex_adapter.proxy_config_args("http://127.0.0.1:8787/kiln/coder")
        assert f"model_provider={codex_adapter.PROXY_PROVIDER}" in args

    def test_carries_the_role_prefixed_base_url(self):
        args = codex_adapter.proxy_config_args("http://127.0.0.1:8787/kiln/coder")
        assert any('base_url="http://127.0.0.1:8787/kiln/coder"' in arg for arg in args)

    def test_pins_the_responses_wire_api(self):
        # Letting Codex default to the chat shape produces a stream neither side can parse.
        args = codex_adapter.proxy_config_args("http://127.0.0.1:8787/kiln/coder")
        assert any('wire_api="responses"' in arg for arg in args)

    def test_uses_the_copied_openai_login(self):
        args = codex_adapter.proxy_config_args("http://127.0.0.1:8787/kiln/coder")
        assert "model_providers.kiln.requires_openai_auth=true" in args

    def test_a_trailing_slash_does_not_double_up(self):
        args = codex_adapter.proxy_config_args("http://127.0.0.1:8787/kiln/coder/")
        assert any(arg.endswith('base_url="http://127.0.0.1:8787/kiln/coder"') for arg in args)

    def test_every_override_is_introduced_by_its_own_flag(self):
        args = codex_adapter.proxy_config_args("http://x/kiln/coder")
        assert args[0::2] == ["-c"] * (len(args) // 2)

    def test_build_command_appends_them(self):
        command = codex_adapter.build_command(
            prompt="p", output_file="/tmp/o.txt", proxy_base_url="http://x/kiln/coder"
        )
        assert command[-4:] == codex_adapter.proxy_config_args("http://x/kiln/coder")[-4:]


class TestFindTurnFailure:
    def test_finds_a_failed_turn(self):
        stream = _stream({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
        assert codex_adapter.find_turn_failure(stream) == "401 Unauthorized"

    def test_a_completed_turn_is_not_a_failure(self):
        stream = _stream({"type": "turn.completed", "usage": {}})
        assert codex_adapter.find_turn_failure(stream) is None

    def test_no_events_at_all_is_not_a_failure(self):
        assert codex_adapter.find_turn_failure("") is None

    def test_non_json_lines_are_ignored(self):
        stream = "progress\n{broken\n" + _stream({"type": "turn.failed"})
        assert codex_adapter.find_turn_failure(stream) == "turn failed"


class TestEventRendering:
    def test_successful_command_execution_is_shown(self):
        event = {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 0},
        }
        assert codex_adapter.render_event(event) == [f"  {codex_adapter.ICON_TOOL} pytest -q"]

    def test_failed_command_execution_shows_the_output(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pytest -q",
                "exit_code": 1,
                "aggregated_output": "1 failed, 2 passed",
            },
        }
        assert codex_adapter.render_event(event) == [
            f"  {codex_adapter.ICON_TOOL_ERROR} 1 failed, 2 passed"
        ]

    def test_file_change_is_shown(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"path": "/p/ping.txt", "kind": "add"}],
            },
        }
        assert codex_adapter.render_event(event) == [
            f"  {codex_adapter.ICON_TOOL} file_change  add /p/ping.txt"
        ]

    def test_agent_message_text_is_shown(self):
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Working on it"},
        }
        assert codex_adapter.render_event(event) == ["    Working on it"]

    def test_empty_agent_message_renders_nothing(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": ""}}
        assert codex_adapter.render_event(event) == []

    def test_item_error_is_shown(self):
        event = {"type": "item.completed", "item": {"type": "error", "message": "boom"}}
        assert codex_adapter.render_event(event) == [f"  {codex_adapter.ICON_TOOL_ERROR} boom"]

    def test_turn_failed_is_shown(self):
        event = {"type": "turn.failed", "error": {"message": "401 Unauthorized"}}
        assert codex_adapter.render_event(event) == [
            f"  {codex_adapter.ICON_TOOL_ERROR} 401 Unauthorized"
        ]

    def test_turn_completed_announces_completion(self):
        assert codex_adapter.render_event({"type": "turn.completed", "usage": {}}) == [
            f"{codex_adapter.ICON_FINISHED} worker finished"
        ]

    def test_bookkeeping_events_render_nothing(self):
        assert codex_adapter.render_event({"type": "thread.started", "thread_id": "x"}) == []


class FakePopen:
    """Stands in for a streaming codex process."""

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
        """
        Replace Popen, capturing how the adapter invoked it and simulating what the real
        `codex exec -o <file>` process would leave behind: `output_text`, if given, is
        written to whichever path the adapter passed via `-o` -- the real signal `run_worker`
        reads its answer from. `shutil.which` is stubbed for determinism, same reasoning as
        the Copilot adapter's tests.
        """
        calls = {}
        monkeypatch.setattr(codex_adapter.shutil, "which", lambda _name: None)

        def _factory(stdout="", stderr="", exc=None, hang=False, returncode=0, output_text=None):
            def _popen(command, **kwargs):
                calls["command"] = command
                calls["kwargs"] = kwargs
                if output_text is not None:
                    output_path = Path(command[command.index("-o") + 1])
                    output_path.write_text(output_text, encoding="utf-8")
                if exc:
                    raise exc
                process = FakePopen(stdout=stdout, stderr=stderr, hang=hang, returncode=returncode)
                calls["process"] = process
                return process

            monkeypatch.setattr(codex_adapter.subprocess, "Popen", _popen)
            # `terminate_tree` shells out to taskkill (Windows) or signals a process group
            # (POSIX); neither belongs in a unit test holding a fake process.
            monkeypatch.setattr(
                codex_adapter.subprocess,
                "run",
                lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
            )
            return calls

        return _factory

    def test_streams_each_line_to_the_pane(self, fake_run, tmp_path):
        fake_run(
            stdout=_stream(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Working"}},
            ),
            output_text="KILN-STATUS: done built it",
        )
        seen = []
        codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=seen.append,
        )
        assert "    Working" in seen

    def test_timeout_kills_the_process(self, fake_run, tmp_path):
        calls = fake_run(hang=True)
        invocation = codex_adapter.run_worker(
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
        fake_run(output_text="KILN-STATUS: done implemented feature")
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done
        assert invocation.result.summary == "implemented feature"

    def test_redirects_stdin_and_separates_streams(self, fake_run, tmp_path):
        calls = fake_run(output_text="KILN-STATUS: done x")
        codex_adapter.run_worker(
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
        fake_run(output_text="KILN-STATUS: blocked missing fixtures")
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.summary == "missing fixtures"

    def test_missing_binary_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(exc=OSError("codex not found"))
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True

    def test_a_failed_turn_is_treated_as_blocked_even_with_a_zero_exit(self, fake_run, tmp_path):
        # Verified live: an auth failure still exits 0 -- turn.failed is the real signal.
        fake_run(
            stdout=_stream({"type": "turn.failed", "error": {"message": "401 Unauthorized"}}),
            output_text="KILN-STATUS: done x",  # must not be trusted once turn.failed fires
        )
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True
        assert "401 Unauthorized" in invocation.result.summary

    def test_nonzero_exit_is_treated_as_blocked(self, fake_run, tmp_path):
        fake_run(stderr="crashed", returncode=1)
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.is_error is True
        assert "crashed" in invocation.result.summary

    def test_no_output_file_is_treated_as_blocked(self, fake_run, tmp_path):
        # output_text=None: the process never writes the -o file at all.
        fake_run(stderr="no output produced")
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert "no output produced" in invocation.result.summary

    def test_missing_sentinel_is_blocked(self, fake_run, tmp_path):
        fake_run(output_text="I finished but forgot the sentinel.")
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.is_done is False
        assert invocation.result.sentinel_found is False

    def test_cost_is_not_tracked_for_this_backend(self, fake_run, tmp_path):
        # No dollar figure exists anywhere in Codex's output -- confirmed live, only token
        # usage.
        fake_run(output_text="KILN-STATUS: done x")
        invocation = codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        assert invocation.cost_usd == 0.0

    def test_the_output_file_is_cleaned_up(self, fake_run, tmp_path):
        calls = fake_run(output_text="KILN-STATUS: done x")
        codex_adapter.run_worker(
            definition=DEFINITION,
            prompt="p",
            cwd=tmp_path,
            model="",
            on_output=lambda _l: None,
        )
        output_path = Path(calls["command"][calls["command"].index("-o") + 1])
        assert not output_path.exists()
