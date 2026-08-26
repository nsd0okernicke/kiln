from __future__ import annotations

import io
import json

import pytest

from kiln.scheduler.domain.worker_prompt import WorkerDefinition
from kiln.scheduler.infrastructure.agents import StreamCapture, pi_adapter

DEFINITION = WorkerDefinition(
    name="coder-worker",
    description="Codes",
    prompt="Follow the coder role.",
)


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _answer(text: str, usage: dict | None = None) -> dict:
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if usage is not None:
        message["usage"] = usage
    return {"type": "message_end", "message": message}


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def test_command_is_ephemeral_isolated_json_mode():
    command = pi_adapter.build_command(model="igate/coder")
    prompt = pi_adapter.build_prompt(definition=DEFINITION, prompt="implement it")

    assert command[:5] == ["pi", "--mode", "json", "--no-session", "--no-approve"]
    assert command[command.index("--model") + 1] == "igate/coder"
    assert command[command.index("--tools") + 1] == pi_adapter.PI_TOOLS
    assert "Follow the coder role." in prompt
    assert "implement it" in prompt
    assert not any("key" in part.lower() for part in command)


def test_large_prompt_is_not_put_on_the_windows_command_line():
    prompt = pi_adapter.build_prompt(definition=DEFINITION, prompt="x" * 100_000)
    command = pi_adapter.build_command(model="igate/coder")

    assert len(prompt) > 100_000
    assert all("x" * 100 not in part for part in command)
    assert sum(map(len, command)) < 1_000


def test_parser_uses_the_last_authoritative_assistant_message():
    stdout = _stream(_answer("draft"), _answer("KILN-STATUS: done complete"))

    assert pi_adapter.parse_cli_output(stdout) == "KILN-STATUS: done complete"


def test_rendering_surfaces_tools_errors_and_text():
    assert pi_adapter.render_event(
        {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "pytest"}}
    ) == [f"  {pi_adapter.ICON_TOOL} bash  pytest"]
    assert pi_adapter.render_event(
        {
            "type": "tool_execution_end",
            "isError": True,
            "result": {"content": [{"type": "text", "text": "failed"}]},
        }
    ) == [f"  {pi_adapter.ICON_TOOL_ERROR} failed"]
    assert pi_adapter.render_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "working"},
        }
    ) == ["    working"]


def test_rendering_handles_session_completion_and_ignored_events():
    assert pi_adapter.render_event({"type": "session"}) == [
        f"{pi_adapter.ICON_SESSION} worker session started"
    ]
    assert pi_adapter.render_event({"type": "agent_settled"}) == [
        f"{pi_adapter.ICON_FINISHED} worker finished"
    ]
    assert pi_adapter.render_event({"type": "tool_execution_end", "isError": False}) == []
    assert pi_adapter.render_event({"type": "turn_start"}) == []


def test_parser_rejects_a_stream_without_a_final_assistant_message():
    with pytest.raises(ValueError, match="no final assistant message"):
        pi_adapter.parse_cli_output(_stream({"type": "agent_end", "messages": []}))


def test_usage_reads_pi_token_names():
    stdout = _stream(
        _answer(
            "done",
            {"input": 11, "output": 7, "cacheRead": 5, "cacheWrite": 3},
        )
    )

    assert pi_adapter.find_usage(stdout).total == 26


@pytest.fixture
def run_with(monkeypatch):
    def install(stdout: str, *, stderr: str = "", returncode: int = 0):
        process = FakeProcess(stdout, stderr, returncode)
        monkeypatch.setattr(pi_adapter.shutil, "which", lambda _name: None)
        monkeypatch.setattr(pi_adapter, "_start_process", lambda _command, _cwd, _prompt: process)
        return process

    return install


def test_successful_worker_returns_kiln_report(run_with, tmp_path):
    run_with(_stream(_answer("KILN-STATUS: done implemented")))

    result = pi_adapter.run_worker(
        definition=DEFINITION,
        prompt="work",
        cwd=tmp_path,
        model="igate/coder",
        on_output=lambda _line: None,
    )

    assert result.is_done
    assert result.result.summary == "implemented"


def test_cli_failure_becomes_a_blocked_invocation(run_with, tmp_path):
    run_with("", stderr="authentication failed", returncode=1)

    result = pi_adapter.run_worker(
        definition=DEFINITION,
        prompt="work",
        cwd=tmp_path,
        model="igate/coder",
        on_output=lambda _line: None,
    )

    assert result.is_error
    assert not result.is_done
    assert result.detail == "authentication failed"


def test_successful_exit_without_a_final_message_is_blocked(run_with, tmp_path):
    run_with(_stream({"type": "agent_end", "messages": []}))

    result = pi_adapter.run_worker(
        definition=DEFINITION,
        prompt="work",
        cwd=tmp_path,
        model="",
        on_output=lambda _line: None,
    )

    assert result.is_error
    assert "no final assistant message" in result.detail


def test_timeout_is_reported_as_blocked(monkeypatch, run_with, tmp_path):
    run_with("")
    monkeypatch.setattr(
        pi_adapter,
        "_capture",
        lambda *_args: StreamCapture("partial", "worker timed out after 1s"),
    )

    result = pi_adapter.run_worker(
        definition=DEFINITION,
        prompt="work",
        cwd=tmp_path,
        model="",
        on_output=lambda _line: None,
    )

    assert result.timed_out
    assert result.raw_output == "partial"


def test_missing_binary_becomes_a_blocked_invocation(monkeypatch, tmp_path):
    monkeypatch.setattr(pi_adapter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        pi_adapter,
        "_start_process",
        lambda _command, _cwd, _prompt: (_ for _ in ()).throw(OSError("not found")),
    )

    result = pi_adapter.run_worker(
        definition=DEFINITION,
        prompt="work",
        cwd=tmp_path,
        model="",
        on_output=lambda _line: None,
    )

    assert result.is_error
    assert "could not launch pi" in result.detail
