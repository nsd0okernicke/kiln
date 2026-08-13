"""
One-shot Claude Code worker invocation.

Every flag here was verified live against Claude Code 2.1.224 (see "Spike results: Claude"
in full-python-plan.md). The non-obvious ones:

- `--strict-mcp-config` with no `--mcp-config`: verified to yield zero MCP tools, which is
  what keeps a worker from reaching kiln-db/kiln-channel and sending its own handoffs.
- `--setting-sources project`: project skills stay available, the operator's *user-global*
  plugin skills do not leak into the worker's context.
- `--agents` + `--agent`: feeds the generated worker definition; its prompt demonstrably
  governs the response.
- `--model` is always passed explicitly. The CLI default is Opus, which measured 5-10x the
  cost of Sonnet on an identical trivial call.
- `--bare` is deliberately NOT used: it would suppress CLAUDE.md, but its auth is strictly
  ANTHROPIC_API_KEY and it fails outright for OAuth/subscription users.
- stdin is redirected from devnull: without it the CLI blocks ~3s waiting for input.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..status_contract import STATUS_BLOCKED, WorkerResult, parse_worker_report
from ..worker_prompt import WorkerDefinition, build_agents_payload
from . import TokenUsage, WorkerInvocation

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 900  # 15 minutes; a hang is indistinguishable from a blocked worker
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# Glyphs for the streamed worker output. Rendering them needs UTF-8 stdout, which
# role_scheduler.enable_unicode_output() guarantees.
ICON_SESSION = "\N{HIGH VOLTAGE SIGN}"
ICON_TOOL = "\N{HAMMER AND WRENCH}"
ICON_FINISHED = "\N{CHEQUERED FLAG}"
ICON_FAILED = "\N{CROSS MARK}"
ICON_TOOL_ERROR = "\N{WARNING SIGN}"

#: The one input field worth showing per tool. `[ICON_TOOL] Bash` alone tells an operator
#: nothing — which command, which file, which pattern is the whole point of watching.
TOOL_DETAIL_FIELD = {
    "Bash": "command",
    "BashOutput": "bash_id",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Skill": "skill",
    "Task": "description",
    "Agent": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "TodoWrite": None,  # a JSON todo array is noise in a pane
}

#: Fallback order for tools not listed above, including any the CLI adds later.
_FALLBACK_FIELDS = ("command", "file_path", "path", "pattern", "query", "url", "description")

#: Long enough for a real command, short enough not to wrap a normal pane.
MAX_DETAIL_CHARS = 140


def build_command(
    *,
    agents_json: str,
    agent_name: str,
    prompt: str,
    model: str,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_budget_usd: float | None = None,
    debug_log: Path | str | None = None,
) -> list[str]:
    """
    Construct the one-shot argv. Pure — spawns nothing.

    `stream-json` rather than a single `json` blob so the worker's progress can be shown in
    the pane as it happens. With plain `json` the CLI emits nothing until it finishes, which
    left the scheduler's pane looking hung for minutes at a time — the same
    "worker output isn't visible" problem that motivated replacing the wrapper.
    `--verbose` is required for streaming in print mode.

    `--debug-file` (when `debug_log` is set) works in `-p` mode too -- verified live, a
    trivial call produced 191 lines of internal trace. Off by default: it's a lot of volume
    for a healthy run, worth paying for only while actively diagnosing a failure.
    """
    command = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--agents", agents_json,
        "--agent", agent_name,
        "--strict-mcp-config",
        "--setting-sources", "project",
        "--permission-mode", permission_mode,
    ]
    if max_budget_usd is not None:
        command += ["--max-budget-usd", str(max_budget_usd)]
    if debug_log is not None:
        command += ["--debug-file", str(debug_log)]
    command.append(prompt)
    return command


def _condense(value: object) -> str:
    """One line, bounded length — a heredoc in a Bash command must not flood the pane."""
    text = " ".join(str(value).split())
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def summarise_tool_use(name: str, payload: dict) -> str:
    """`Bash` + {'command': 'pytest -q'} -> 'Bash  pytest -q'."""
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

    Only the parts an operator watching the pane cares about: what the worker said, which
    tools it reached for and with what, and which of those failed. Everything else is
    bookkeeping.
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
                    # Plain indent, no glyph: prose is the bulk of the output and a marker
                    # on every line would be noise rather than signal.
                    lines.extend(f"    {line}" for line in text.splitlines())
            elif block_type == "tool_use":
                name = str(block.get("name", "tool"))
                lines.append(f"  {ICON_TOOL} {summarise_tool_use(name, block.get('input') or {})}")
        return lines

    if kind == "user":
        # Tool results are far too voluminous to show wholesale, but a *failing* tool is
        # exactly what an operator needs to see: it is usually why the worker ends up
        # blocked, and without this the pane shows a silent retry loop.
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

    Scans for JSON objects line by line rather than parsing the whole stream, because the
    CLI can emit an unstructured notice ahead of the events. The last `result` event wins;
    if none is present, the last parseable object is used so a caller still gets something
    to report.
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
    raise ValueError("no JSON envelope found in claude output")


#: Anthropic wire-format usage keys -> TokenUsage fields. The cache keys are spelled
#: `cache_*_input_tokens` on the wire but kept separate from `input_tokens` here, because
#: they are priced differently -- see TokenUsage's own note.
_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def _as_int(value: object) -> int | None:
    """A usage count, or None when the field is absent or not a number.

    `bool` is excluded deliberately: it is a subclass of `int`, so a JSON `true` would
    otherwise silently become a token count of 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_usage(envelope: dict) -> TokenUsage | None:
    """
    Extract token counts from a `result` event, or None when it reports none.

    None rather than a zeroed TokenUsage: "this backend told us nothing" and "this call used
    no tokens" are different facts, and only the second one is safe to display. Unrecognised
    or non-numeric fields are skipped rather than defaulted, so a wire-format change degrades
    to a missing number instead of a wrong one.
    """
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
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_budget_usd: float | None = None,
    on_output: Callable[[str], None] | None = None,
    debug_base: Path | str | None = None,
) -> WorkerInvocation:
    """
    Run one worker to completion, streaming its progress, and parse its verdict.

    Never raises for a worker-level failure: a timeout, a crash, a malformed stream and an
    explicit `KILN-STATUS: blocked` all converge on a blocked WorkerInvocation, because the
    scheduler's retry/escalation policy handles them identically.

    `on_output` receives each rendered line (defaults to printing to the pane), so tests can
    assert on what an operator would see without capturing stdout.

    `debug_base` (when set) becomes `{debug_base}.log` for `--debug-file` -- one flat file,
    unlike Copilot's own `--log-dir` which wants a directory.
    """
    command = build_command(
        # include_tools: the worker file's declared tool list is honoured rather than
        # dropped -- see build_agents_payload. Verified live against Claude Code.
        agents_json=build_agents_payload(definition, include_tools=True),
        agent_name=definition.name,
        prompt=prompt,
        model=model,
        permission_mode=permission_mode,
        max_budget_usd=max_budget_usd,
        debug_log=f"{debug_base}.log" if debug_base is not None else None,
    )

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
        )
    except OSError as exc:
        log.error("could not launch worker %s: %s", definition.name, exc)
        return _blocked(f"could not launch claude: {exc}", "", is_error=True)

    def _abort() -> None:
        timed_out.set()
        process.kill()

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
        process.wait()
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
        detail = stderr.strip() or "claude produced no parseable output"
        log.error("worker %s produced no result event: %s", definition.name, detail)
        return _blocked(detail, stdout, is_error=True)

    text = str(envelope.get("result", ""))
    cost = float(envelope.get("total_cost_usd") or 0.0)
    # A failed turn still burned tokens, so usage is read before the error branch rather
    # than only on the success path -- otherwise the most expensive cycles (the ones that
    # retry) would be the ones missing from the totals.
    tokens = parse_usage(envelope)

    if envelope.get("is_error"):
        log.error("worker %s reported an error: %s", definition.name, text)
        return _blocked(
            text or "claude reported is_error", text,
            cost_usd=cost, is_error=True, tokens=tokens,
        )

    result = parse_worker_report(text)
    log.info(
        "worker %s finished: status=%s sentinel=%s cost=$%.4f tokens=%s",
        definition.name, result.status, result.sentinel_found, cost,
        tokens.total if tokens else "-",
    )
    return WorkerInvocation(result=result, raw_output=text, cost_usd=cost, tokens=tokens)
