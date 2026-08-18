"""
One-shot Grok CLI worker invocation.

Every flag here was verified live against `grok` 1.0.0 (3cd0d0cbce, stable) in scratch spikes
this session, the same methodology claude_adapter.py's own flags were verified with. The
non-obvious ones:

- `--output-format streaming-messages-json`: documented by the CLI itself as "NDJSON in the
  Anthropic Messages API wire format" -- and verified live, its event shapes (`system`/
  `assistant`/`user`/`result`, `tool_use`/`tool_result` content blocks) are structurally
  identical to what claude_adapter.py already parses, right down to the `result` event's
  `is_error`/`result`/`total_cost_usd` fields. Only the tool *names* differ (grok's are
  lowercase/snake_case: `write`, `run_terminal_command`, `read_file`, ... vs Claude's
  PascalCase), so this module needs its own tool-name table but can reuse the rest of the
  parsing/rendering shape.
- `--agents '<json>' --agent <name>`: the same inline-definition mechanism Claude uses
  (`{name: {description, prompt}}`) -- no per-worktree file mirroring needed, and
  `worker_prompt.build_agents_payload()` is reused unchanged.
- `--always-approve`: required for non-interactive execution, same role as Claude's
  `--permission-mode bypassPermissions`.
- `--no-subagents`: disables grok's own `spawn_subagent` tool -- the isolation-from-recursive-
  delegation equivalent of the Claude worker having no `Agent` tool, and Codex's
  `mcp_servers = {}`.
- **Grok reports real USD cost** (`total_cost_usd`, same field name as Claude, confirmed live)
  -- unlike the Copilot/Codex adapters, `cost_usd` here is genuinely populated, not left at
  the dataclass default.
- MCP isolation: verified live, a fresh session reports `"mcp_servers":[]` with nothing
  configured (`grok mcp list` confirms the same). Kiln has never wired grok into any MCP
  registration, so there is nothing to defensively disable today -- but note this CLI has no
  `--strict-mcp-config`-equivalent override flag the way Claude/Copilot do. If a future
  feature ever registers an MCP server globally for grok, this adapter needs revisiting.
- The resolved binary is looked up with `shutil.which` before `Popen`, same reasoning as the
  Copilot/Codex adapters: nothing guarantees `grok` ships as a native `.exe` everywhere the
  way it happens to on this machine.
- stdin is redirected from devnull, same reasoning as the Claude adapter.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..status_contract import STATUS_BLOCKED, WorkerResult, parse_worker_report
from ..worker_prompt import WorkerDefinition, build_agents_payload
from . import REAP_TIMEOUT_SEC, TokenUsage, WorkerInvocation, terminate_tree

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 900  # 15 minutes; a hang is indistinguishable from a blocked worker

ICON_SESSION = "\N{HIGH VOLTAGE SIGN}"
ICON_TOOL = "\N{HAMMER AND WRENCH}"
ICON_FINISHED = "\N{CHEQUERED FLAG}"
ICON_FAILED = "\N{CROSS MARK}"
ICON_TOOL_ERROR = "\N{WARNING SIGN}"

#: The one input field worth showing per tool -- grok's own (lowercase/snake_case) tool
#: names, verified live against its default tool list. `None` means "noise, show the bare
#: name" (mirrors Claude's TodoWrite entry).
TOOL_DETAIL_FIELD = {
    "write": "file_path",
    "search_replace": "file_path",
    "read_file": "file_path",
    "list_dir": "path",
    "grep": "pattern",
    "run_terminal_command": "command",
    "web_search": "query",
    "web_fetch": "url",
    "search_tool": "query",
    "spawn_subagent": "description",
    "todo_write": None,
    "kill_command_or_subagent": None,
    "get_command_or_subagent_output": None,
    "scheduler_create": None,
    "scheduler_delete": None,
    "scheduler_list": None,
    "monitor": None,
    "use_tool": None,
    "workflow": None,
    "enter_plan_mode": None,
    "exit_plan_mode": None,
    "ask_user_question": None,
}

#: Fallback order for tools not listed above, including any the CLI adds later.
_FALLBACK_FIELDS = ("command", "file_path", "path", "pattern", "query", "url", "description")

#: Long enough for a real command, short enough not to wrap a normal pane.
MAX_DETAIL_CHARS = 140


def build_command(
    *, agents_json: str, agent_name: str, prompt: str, model: str = "",
) -> list[str]:
    """Construct the one-shot argv. Pure -- spawns nothing."""
    command = [
        "grok",
        "-p", prompt,
        "--agents", agents_json,
        "--agent", agent_name,
        "--always-approve",
        "--output-format", "streaming-messages-json",
        "--no-subagents",
    ]
    if model:
        command += ["-m", model]
    return command


def _condense(value: object) -> str:
    """One line, bounded length -- a heredoc in a Bash command must not flood the pane."""
    text = " ".join(str(value).split())
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def summarise_tool_use(name: str, payload: dict) -> str:
    """`run_terminal_command` + {'command': 'pytest -q'} -> 'run_terminal_command  pytest -q'."""
    if name in TOOL_DETAIL_FIELD:
        field = TOOL_DETAIL_FIELD[name]
        detail = payload.get(field) if field else None
    else:
        detail = next(
            (payload[key] for key in _FALLBACK_FIELDS if payload.get(key)), None
        )
    return f"{name}  {_condense(detail)}" if detail else name


def render_event(event: dict) -> list[str]:
    """
    Turn one stream event into human-readable pane lines.

    Same shape as claude_adapter.render_event -- the wire format is the same Anthropic
    Messages API events, just with grok's own tool names.
    """
    kind = event.get("type")

    if kind == "system" and event.get("subtype") == "init":
        return [f"{ICON_SESSION} worker session started"]

    if kind == "assistant":
        lines: list[str] = []
        for block in event.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    lines.extend(f"    {line}" for line in text.splitlines())
            elif block_type == "tool_use":
                name = str(block.get("name", "tool"))
                lines.append(f"  {ICON_TOOL} {summarise_tool_use(name, block.get('input') or {})}")
        return lines

    if kind == "user":
        lines = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_result" and block.get("is_error"):
                lines.append(f"  {ICON_TOOL_ERROR} {_condense(_result_text(block))}")
        return lines

    if kind == "result":
        cost = event.get("total_cost_usd") or 0.0
        icon = ICON_FAILED if event.get("is_error") else ICON_FINISHED
        return [f"{icon} worker finished (cost ${float(cost):.4f})"]

    return []


def _result_text(block: dict) -> str:
    """Tool result content is either a plain string or a list of typed blocks."""
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "tool failed")


def parse_cli_output(stdout: str) -> dict:
    """
    Pull the final `result` event out of a captured stream.

    Same logic as claude_adapter.parse_cli_output -- scans line by line, last `result` event
    wins, falls back to the last parseable object.
    """
    result: dict | None = None
    fallback: dict | None = None

    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        fallback = event
        if event.get("type") == "result":
            result = event

    if result is not None:
        return result
    if fallback is not None:
        return fallback
    raise ValueError("no JSON envelope found in grok output")


#: Grok emits the Anthropic Messages API wire format (see the module docstring), so the
#: usage keys are the same ones claude_adapter reads. Kept as its own table rather than
#: imported from there: these adapters deliberately duplicate small helpers (`_condense`,
#: `render_event`) so one CLI changing its output cannot break another's parsing.
_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def _as_int(value: object) -> int | None:
    """A usage count, or None when absent or not a number (`bool` excluded — it is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_usage(envelope: dict) -> TokenUsage | None:
    """Extract token counts from a `result` event, or None when it reports none."""
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None

    values = {}
    for wire_name, field_name in _USAGE_FIELDS.items():
        count = _as_int(usage.get(wire_name))
        if count is not None:
            values[field_name] = count
    return TokenUsage(**values) if values else None


def _blocked(summary: str, raw: str, **kwargs) -> WorkerInvocation:
    return WorkerInvocation(
        result=WorkerResult(status=STATUS_BLOCKED, summary=summary, sentinel_found=False),
        raw_output=raw,
        detail=summary,
        **kwargs,
    )


def _default_emit(line: str) -> None:
    """Worker output goes straight to the pane, unbuffered, so progress is visible live."""
    print(line, flush=True)


def run_worker(
    *,
    definition: WorkerDefinition,
    prompt: str,
    cwd: str | Path,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    on_output: Callable[[str], None] | None = None,
    debug_base: Path | str | None = None,
) -> WorkerInvocation:
    """
    Run one worker to completion, streaming its progress, and parse its verdict.

    Same failure-convergence contract as claude_adapter.run_worker: a timeout, a crash, a
    malformed stream and an explicit `KILN-STATUS: blocked` all converge on a blocked
    WorkerInvocation, because the scheduler's retry/escalation policy handles them identically.

    `on_output` receives each rendered line (defaults to printing to the pane), so tests can
    assert on what an operator would see without capturing stdout.

    `debug_base` is accepted for signature parity with the other adapters (the scheduler
    dispatches to whichever backend a role picked without knowing which). Unused here: no
    `--debug-file`/`--log-dir`-equivalent flag has been found in `grok`'s help output.
    """
    command = build_command(
        # No `include_tools` here, unlike the Claude adapter: grok takes the same
        # `--agents` shape but has not been checked for the `tools` key, and Claude Code's
        # failure mode for a malformed one is to discard the whole agent definition without
        # saying so. Not worth risking a silently persona-less worker for a size saving --
        # spike grok first, then turn it on.
        agents_json=build_agents_payload(definition),
        agent_name=definition.name,
        prompt=prompt,
        model=model,
    )
    resolved = shutil.which(command[0])
    if resolved:
        command[0] = resolved

    emit = on_output or _default_emit
    timed_out = threading.Event()

    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # kept separate: merging would corrupt the event stream
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            bufsize=1,  # line buffered, so the pane updates as the worker works
            # A new session so the whole group can be signalled on timeout without
            # touching the scheduler's own (POSIX); accepted and ignored on Windows.
            start_new_session=True,
        )
    except OSError as exc:
        log.error("could not launch worker %s: %s", definition.name, exc)
        return _blocked(f"could not launch grok: {exc}", "", is_error=True)

    def _abort() -> None:
        timed_out.set()
        terminate_tree(process)

    # A watchdog rather than a per-line deadline: a worker that hangs producing no output at
    # all would otherwise block on readline forever.
    watchdog = threading.Timer(timeout, _abort)
    watchdog.daemon = True
    watchdog.start()

    captured: list[str] = []
    try:
        for line in process.stdout:  # type: ignore[union-attr]
            captured.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            for rendered in render_event(event):
                emit(rendered)
        try:
            # Bounded: the pipe closing means the child is finishing, not that it has
            # finished. An unbounded wait here reintroduces exactly the hang the
            # watchdog exists to end.
            process.wait(timeout=REAP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            terminate_tree(process)
    finally:
        watchdog.cancel()

    stdout = "".join(captured)

    if timed_out.is_set():
        log.error("worker %s exceeded %ss", definition.name, timeout)
        return _blocked(f"worker timed out after {timeout}s", stdout, timed_out=True)

    stderr = (process.stderr.read() if process.stderr else "") or ""
    try:
        envelope = parse_cli_output(stdout)
    except ValueError:
        detail = stderr.strip() or "grok produced no parseable output"
        log.error("worker %s produced no result event: %s", definition.name, detail)
        return _blocked(detail, stdout, is_error=True)

    text = str(envelope.get("result", ""))
    cost = float(envelope.get("total_cost_usd") or 0.0)
    # Read before the error branch: a failed turn still burned tokens (see claude_adapter).
    tokens = parse_usage(envelope)

    if envelope.get("is_error"):
        log.error("worker %s reported an error: %s", definition.name, text)
        return _blocked(
            text or "grok reported is_error", text,
            cost_usd=cost, is_error=True, tokens=tokens,
        )

    result = parse_worker_report(text)
    log.info(
        "worker %s finished: status=%s sentinel=%s cost=$%.4f tokens=%s",
        definition.name, result.status, result.sentinel_found, cost,
        tokens.total if tokens else "-",
    )
    return WorkerInvocation(result=result, raw_output=text, cost_usd=cost, tokens=tokens)
