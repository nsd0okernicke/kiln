"""
One-shot GitHub Copilot CLI worker invocation.

Every flag here was verified live against GitHub Copilot CLI 1.0.78 in scratch spikes this
session, the same methodology claude_adapter.py's own flags were verified with. The
non-obvious ones:

- `--disable-mcp-server kiln-db --disable-builtin-mcps`: `workspace.prepare_agent_configs()`
  writes `kiln-db` into Copilot's *global* `~/.copilot/mcp-config.json` whenever any role uses
  copilot, so an unflagged worker invocation would otherwise have live handoff-queue access --
  the same isolation break Claude's `--strict-mcp-config` exists to prevent. Verified live:
  with both flags, a session shows zero MCP-related events at all.
- `--agent <name>`: resolves the already-generated `.github/agents/<name>.agent.md` in `cwd`
  by name. Unlike Claude, no inline JSON payload is needed -- Copilot reads the worker's
  actual instructions straight off disk.
- `--output-format json`: JSONL, one event per line. The final answer is the **last**
  `assistant.message` event with non-empty `content` -- verified live: a turn that calls
  tools first emits an `assistant.message` with empty `content` (the tool calls live in
  `toolRequests` instead), then a second, final `assistant.message` with the real reply and
  no tool requests. "Last one with content" is what `parse_cli_output` looks for.
- No dollar cost anywhere in the output -- only `usage.premiumRequests`/token counts in the
  final `result` event. `cost_usd` is left at its dataclass default (`0.0`), same as an
  untracked wrapper-mode role; the dashboard and pane status bar already treat that as
  "no cost data" rather than displaying a real, misleadingly-precise number.
- stdin is redirected from devnull, same reasoning as the Claude adapter.
- The resolved binary is looked up with `shutil.which` before `Popen` rather than handed the
  bare `"copilot"` string. Verified live: Copilot CLI installs as `copilot.cmd` (an npm shim,
  not a native `.exe`), and Windows' `CreateProcess` -- what `subprocess.Popen` calls without
  `shell=True` -- does not apply `PATHEXT` resolution to a bare command name the way a real
  shell does, so the bare name fails with `WinError 2` even though `copilot` is genuinely on
  `PATH`. `claude_adapter.py` doesn't need this (Claude Code ships a native `.exe`), but a
  worker invocation must not depend on which of the two happens to be true.
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
from ..worker_prompt import WorkerDefinition
from . import WorkerInvocation

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 900  # 15 minutes; a hang is indistinguishable from a blocked worker

ICON_TOOL = "\N{HAMMER AND WRENCH}"
ICON_TOOL_ERROR = "\N{WARNING SIGN}"
ICON_FINISHED = "\N{CHEQUERED FLAG}"

#: Long enough for a real command, short enough not to wrap a normal pane.
MAX_DETAIL_CHARS = 140


def build_command(
    *, agent_name: str, prompt: str, model: str = "", log_dir: Path | str | None = None
) -> list[str]:
    """
    Construct the one-shot argv. Pure -- spawns nothing.

    `--log-dir`/`--log-level all` (when `log_dir` is set) write Copilot's own internal trace
    -- verified live: a trivial call produced a 200KB+ log including `AUTO_APPROVAL` state and
    trust-check decisions, exactly the detail `parse_cli_output`'s bare "Permission denied and
    could not request permission from user" doesn't carry. Off by default, same reasoning as
    the Claude adapter's `--debug-file`: expensive to write for a healthy run.

    `--allow-tool=read/write/shell` alongside `--allow-all`: decompiling the shipped CLI
    bundle (`@github/copilot-win32-x64/app.js` 1.0.79) showed that `--allow-all`'s
    `approveAllToolPermissionRequests` is a runtime flag that an enterprise managed-settings
    re-resolution (auth refresh, or an hourly timer) can silently zero out mid-session on a
    fail-closed policy check, while an explicit `--allow-tool` grant is stored under a
    separate `rules` key untouched by that same flip -- this is the confirmed mechanism
    behind long scheduler-mode sessions eventually
    denying every write/MCP call ("...and could not request permission from user") while reads
    (gated by the still-unaffected `approveAllReadPermissionRequests`) keep working. Verified
    live that the flag combination itself parses and runs cleanly; matches the three tool
    categories the generated `.github/agents/<role>-worker.agent.md` already declares.
    """
    command = [
        "copilot",
        "-p", prompt,
        "--agent", agent_name,
        "--allow-all",
        "--allow-tool=read", "--allow-tool=write", "--allow-tool=shell",
        "--output-format", "json",
        "--disable-mcp-server", "kiln-db",
        "--disable-builtin-mcps",
    ]
    if model:
        command += ["--model", model]
    if log_dir is not None:
        command += ["--log-dir", str(log_dir), "--log-level", "all"]
    return command


def _condense(value: object) -> str:
    """One line, bounded length -- a heredoc in a Bash command must not flood the pane."""
    text = " ".join(str(value).split())
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def render_event(event: dict) -> list[str]:
    """
    Turn one stream event into human-readable pane lines.

    Deliberately ignores `assistant.reasoning_delta`/`assistant.message_delta`: the
    accumulated final text already arrives whole in the closing `assistant.message` event, so
    streaming the deltas too would show everything twice.
    """
    kind = event.get("type")
    data = event.get("data") or {}

    if kind == "tool.execution_start":
        name = str(data.get("toolName", "tool"))
        arguments = data.get("arguments") or {}
        detail = next(iter(arguments.values()), None) if arguments else None
        label = f"{name}  {_condense(detail)}" if detail else name
        return [f"  {ICON_TOOL} {label}"]

    if kind == "tool.execution_complete" and not data.get("success", True):
        result = data.get("result") or {}
        return [f"  {ICON_TOOL_ERROR} {_condense(result.get('content', 'tool failed'))}"]

    if kind == "assistant.message" and str(data.get("content", "")).strip():
        text = str(data["content"]).strip()
        return [f"    {line}" for line in text.splitlines()]

    if kind == "result":
        return [f"{ICON_FINISHED} worker finished"]

    return []


def parse_cli_output(stdout: str) -> dict:
    """
    Pull the last non-empty `assistant.message` event out of a captured JSONL stream.

    A turn that calls tools emits intermediate `assistant.message` events with empty
    `content` before the final one -- "last event of this type" alone is not enough, it must
    also be non-empty, or a tool-heavy turn would report an empty answer.
    """
    result: dict | None = None
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant.message" and str(
            (event.get("data") or {}).get("content", "")
        ).strip():
            result = event

    if result is not None:
        return result
    raise ValueError("no assistant.message with content found in copilot output")


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

    Same failure-convergence contract as `claude_adapter.run_worker`: a timeout, a crash, a
    malformed stream, a nonzero exit and an explicit `KILN-STATUS: blocked` all become a
    blocked `WorkerInvocation`, never a raised exception -- the scheduler's retry/escalation
    policy handles them identically regardless of which backend produced them.

    `on_output` receives each rendered line (defaults to printing to the pane), so tests can
    assert on what an operator would see without capturing stdout.

    `debug_base` (when set) becomes the `--log-dir` -- Copilot writes its own filename inside
    it, unlike Claude's `--debug-file` which wants an exact file path.
    """
    if debug_base is not None:
        Path(debug_base).mkdir(parents=True, exist_ok=True)
    command = build_command(
        agent_name=definition.name, prompt=prompt, model=model, log_dir=debug_base
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
        )
    except OSError as exc:
        log.error("could not launch worker %s: %s", definition.name, exc)
        return _blocked(f"could not launch copilot: {exc}", "", is_error=True)

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

    if process.returncode != 0:
        detail = stderr.strip() or f"copilot exited {process.returncode}"
        log.error("worker %s failed: %s", definition.name, detail)
        return _blocked(detail, stdout, is_error=True)

    try:
        envelope = parse_cli_output(stdout)
    except ValueError:
        # A session can exit 0 and still never emit a non-empty assistant.message -- observed
        # live: 8+ minutes and real AI credits spent, zero file changes, no closing reply.
        # copilot's own end-of-session summary (Changes/AI Credits/Tokens/Resume) lands on
        # stderr; showing the raw stat block with no framing reads as a crash, not "it ran and
        # gave up" -- the event count says whether it did anything at all before that.
        event_count = sum(1 for line in stdout.splitlines() if line.strip().startswith("{"))
        stats = stderr.strip()
        detail = f"copilot's session ended with no final reply ({event_count} stream events seen)"
        if stats:
            detail += f" -- its own summary: {stats}"
        log.error("worker %s produced no result event: %s", definition.name, detail)
        return _blocked(detail, stdout, is_error=True)

    text = str((envelope.get("data") or {}).get("content", ""))
    result = parse_worker_report(text)
    log.info(
        "worker %s finished: status=%s sentinel=%s",
        definition.name, result.status, result.sentinel_found,
    )
    return WorkerInvocation(result=result, raw_output=text)
