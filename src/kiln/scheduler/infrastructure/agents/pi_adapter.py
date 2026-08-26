"""One-shot Pi coding-agent adapter using Pi's documented JSON event stream."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ...domain.status_contract import STATUS_BLOCKED, WorkerResult, parse_worker_report
from ...domain.worker_prompt import WorkerDefinition
from . import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    TokenUsage,
    Watchdog,
    WorkerInvocation,
    capture_json_stream,
    terminate_tree,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 900
ICON_SESSION = "\N{HIGH VOLTAGE SIGN}"
ICON_TOOL = "\N{HAMMER AND WRENCH}"
ICON_TOOL_ERROR = "\N{WARNING SIGN}"
ICON_FINISHED = "\N{CHEQUERED FLAG}"
MAX_DETAIL_CHARS = 140
PI_TOOLS = "read,write,edit,bash,grep,find,ls"


def build_command(*, model: str = "") -> list[str]:
    """Build an ephemeral, non-interactive Pi invocation without exposing credentials."""
    command = [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-approve",
        "--tools",
        PI_TOOLS,
    ]
    if model:
        command += ["--model", model]
    return command


def build_prompt(*, definition: WorkerDefinition, prompt: str) -> str:
    """Keep the potentially large worker context off the Windows command line."""
    return f"{definition.prompt}\n\n# Current Kiln handoff\n\n{prompt}".strip()


def _condense(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 1] + "…"


def render_event(event: dict) -> list[str]:
    handlers = {
        "session": _render_session,
        "tool_execution_start": _render_tool_start,
        "tool_execution_end": _render_tool_end,
        "message_update": _render_message_update,
        "agent_settled": _render_finished,
        "agent_end": _render_finished,
    }
    event_type = event.get("type")
    handler = handlers.get(event_type) if isinstance(event_type, str) else None
    return handler(event) if handler else []


def _render_session(_event: dict) -> list[str]:
    return [f"{ICON_SESSION} worker session started"]


def _render_tool_start(event: dict) -> list[str]:
    name = str(event.get("toolName") or "tool")
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    detail = next(iter(args.values()), None) if args else None
    suffix = f"  {_condense(detail)}" if detail is not None else ""
    return [f"  {ICON_TOOL} {name}{suffix}"]


def _render_tool_end(event: dict) -> list[str]:
    if not event.get("isError"):
        return []
    return [f"  {ICON_TOOL_ERROR} {_result_text(event.get('result')) or 'tool failed'}"]


def _render_message_update(event: dict) -> list[str]:
    delta = event.get("assistantMessageEvent") or {}
    if delta.get("type") != "text_delta" or not delta.get("delta"):
        return []
    return [f"    {_condense(delta['delta'])}"]


def _render_finished(_event: dict) -> list[str]:
    return [f"{ICON_FINISHED} worker finished"]


def _json_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _result_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    return _content_text(value.get("content"))


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(_text_items(content)).strip()


def _text_items(content: list) -> list[str]:
    return [
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]


def parse_cli_output(stdout: str) -> str:
    """Return the final authoritative assistant message from Pi's JSONL stream."""
    result = ""
    for event in _json_events(stdout):
        text = _assistant_message_text(event)
        if text:
            result = text
    if not result:
        raise ValueError("no final assistant message found in pi output")
    return result


def _assistant_message_text(event: dict) -> str:
    if event.get("type") != "message_end":
        return ""
    message = event.get("message") or {}
    if message.get("role") != "assistant":
        return ""
    return _result_text(message)


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _usage(payload: object) -> TokenUsage | None:
    if not isinstance(payload, dict):
        return None
    tokens = TokenUsage(
        input_tokens=_count(payload.get("input")),
        output_tokens=_count(payload.get("output")),
        cache_read_tokens=_count(payload.get("cacheRead")),
        cache_creation_tokens=_count(payload.get("cacheWrite")),
    )
    return tokens if tokens.total else None


def find_usage(stdout: str) -> TokenUsage | None:
    latest = None
    for event in _json_events(stdout):
        found = _usage(event.get("usage"))
        if found is None and isinstance(event.get("message"), dict):
            found = _usage(event["message"].get("usage"))
        if found is not None:
            latest = found
    return latest


def _blocked(summary: str, raw: str, **kwargs) -> WorkerInvocation:
    return WorkerInvocation(
        result=WorkerResult(status=STATUS_BLOCKED, summary=summary, sentinel_found=False),
        raw_output=raw,
        detail=summary,
        **kwargs,
    )


def _start_process(command: list[str], cwd: str | Path, prompt: str) -> subprocess.Popen:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(prompt)
    process.stdin.close()
    return process


def run_worker(
    *,
    definition: WorkerDefinition,
    prompt: str,
    cwd: str | Path,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT_SEC,
    on_output: Callable[[str], None] | None = None,
    debug_base: Path | str | None = None,
) -> WorkerInvocation:
    """Run Pi once and converge all CLI failures into Kiln's blocked result."""
    del debug_base  # Pi has no equivalent debug-output flag in the verified CLI contract.
    command = build_command(model=model)
    worker_prompt = build_prompt(definition=definition, prompt=prompt)
    resolved = shutil.which(command[0])
    if resolved:
        command[0] = resolved
    process = _launch(command, cwd, worker_prompt)
    if isinstance(process, WorkerInvocation):
        return process
    capture = _capture(process, timeout, idle_timeout, on_output)
    return _captured_invocation(process, capture.stdout, capture.timeout_reason)


def _launch(
    command: list[str], cwd: str | Path, prompt: str
) -> subprocess.Popen | WorkerInvocation:
    try:
        return _start_process(command, cwd, prompt)
    except OSError as exc:
        return _blocked(f"could not launch pi: {exc}", "", is_error=True)


def _capture(process, timeout, idle_timeout, on_output):
    return capture_json_stream(
        process,
        timeout=timeout,
        idle_timeout=idle_timeout,
        render_event=render_event,
        emit=on_output or (lambda line: print(line, flush=True)),
        watchdog_factory=Watchdog,
        terminate=terminate_tree,
    )


def _captured_invocation(
    process: subprocess.Popen, stdout: str, timeout_reason: str | None
) -> WorkerInvocation:
    if timeout_reason:
        return _blocked(timeout_reason, stdout, timed_out=True)
    return _completed_process(process, stdout)


def _completed_process(process: subprocess.Popen, stdout: str) -> WorkerInvocation:
    stderr = (process.stderr.read() if process.stderr else "") or ""
    tokens = find_usage(stdout)
    if process.returncode != 0:
        detail = stderr.strip() or f"pi exited {process.returncode}"
        return _blocked(detail, stdout, is_error=True, tokens=tokens)
    return _successful_process(stdout, stderr, tokens)


def _successful_process(stdout: str, stderr: str, tokens: TokenUsage | None) -> WorkerInvocation:
    try:
        text = parse_cli_output(stdout)
    except ValueError as exc:
        detail = stderr.strip() or str(exc)
        return _blocked(detail, stdout, is_error=True, tokens=tokens)
    return WorkerInvocation(result=parse_worker_report(text), raw_output=text, tokens=tokens)
