"""
One-shot Codex CLI worker invocation.

Every flag here was verified live against `codex-cli` 0.147.0 in scratch spikes this session,
the same methodology claude_adapter.py's own flags were verified with. The non-obvious ones:

- `codex exec` has **no `--agent <name>` flag** the way Copilot does -- it can't load a saved
  custom agent by name, only a raw prompt. So the worker's persona (the generated
  `.codex/agents/<role>-worker.toml`'s `developer_instructions`, already parsed into
  `WorkerDefinition.prompt` by `worker_prompt.parse_toml_worker_definition`) is embedded
  directly ahead of the task prompt, rather than referenced by name.
- `-o/--output-last-message <file>`: writes the final agent message straight to a file --
  verified live to be simpler and more reliable than scanning the JSON stream for it the way
  the Claude/Copilot adapters have to.
- `--ignore-user-config`: skips `$CODEX_HOME/config.toml` (auth still comes from
  `CODEX_HOME`/its default). The per-role `config.toml` `workspace.prepare_agent_configs()`
  writes is meant for the *wrapper*'s interactive session (trust records, MCP servers); this
  one-shot call doesn't need or want any of that, so it's simplest to ignore it outright
  rather than depend on its contents being worker-safe.
- **No `CODEX_HOME` override on purpose.** Verified live: a fresh, empty `CODEX_HOME`
  directory gets `401 Unauthorized` immediately -- `auth.json` genuinely has to already be in
  that exact directory, and nothing in this codebase copies it there per role. The wrapper's
  isolated `paths.codex_home(role)` exists to protect the user's real `~/.codex/config.toml`
  from being overwritten, which `--ignore-user-config` already makes irrelevant to this call.
  Reusing the ambient (already-authenticated) `CODEX_HOME` is what makes this runnable today
  without a separate login per role. If concurrent scheduler roles calling `codex exec`
  against the same session/state files ever proves to be a real contention problem, revisit
  this -- don't "fix" it into isolation by default, that reintroduces the 401.
- `--dangerously-bypass-approvals-and-sandbox`: matches the flag the wrapper-mode
  `_codex_command` in `launcher/commands.py` already uses.
- No dollar cost anywhere in the output -- only token usage (`turn.completed.usage`).
  `cost_usd` is left at its dataclass default (`0.0`), same rationale as the Copilot adapter.
- stdin is redirected from devnull, same reasoning as the Claude adapter.
- The resolved binary is looked up with `shutil.which` before `Popen` rather than handed the
  bare `"codex"` string, same as the Copilot adapter -- `codex.exe` happens to be a native
  executable on this machine so the bare name works either way, but nothing guarantees that
  everywhere Codex CLI installs itself, and the resolution is free.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from ..status_contract import STATUS_BLOCKED, WorkerResult, parse_worker_report
from ..worker_prompt import WorkerDefinition
from . import TokenUsage, WorkerInvocation

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 900  # 15 minutes; a hang is indistinguishable from a blocked worker

ICON_TOOL = "\N{HAMMER AND WRENCH}"
ICON_TOOL_ERROR = "\N{WARNING SIGN}"
ICON_FINISHED = "\N{CHEQUERED FLAG}"

#: Long enough for a real command, short enough not to wrap a normal pane.
MAX_DETAIL_CHARS = 140


def build_full_prompt(definition: WorkerDefinition, task_prompt: str) -> str:
    """
    Persona + task, concatenated -- the closest equivalent to Claude's `--agents` payload or
    Copilot's `--agent` flag that a raw `codex exec <prompt>` call has available.
    """
    return f"{definition.prompt}\n\n---\n\n{task_prompt}"


def build_command(*, prompt: str, output_file: str | Path, model: str = "") -> list[str]:
    """Construct the one-shot argv. Pure -- spawns nothing."""
    command = [
        "codex",
        "exec",
        prompt,
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "-o", str(output_file),
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


def render_event(event: dict) -> list[str]:
    """Turn one stream event into human-readable pane lines."""
    kind = event.get("type")

    if kind == "item.completed":
        item = event.get("item") or {}
        item_type = item.get("type")

        if item_type == "command_execution":
            exit_code = item.get("exit_code")
            if exit_code not in (0, None):
                output = item.get("aggregated_output", "")
                return [f"  {ICON_TOOL_ERROR} {_condense(output or 'command failed')}"]
            return [f"  {ICON_TOOL} {_condense(item.get('command', 'command'))}"]

        if item_type == "file_change":
            paths = ", ".join(
                f"{change.get('kind', '?')} {change.get('path', '?')}"
                for change in item.get("changes", [])
            )
            return [f"  {ICON_TOOL} file_change  {_condense(paths)}"]

        if item_type == "agent_message":
            text = str(item.get("text", "")).strip()
            return [f"    {line}" for line in text.splitlines()] if text else []

        if item_type == "error":
            return [f"  {ICON_TOOL_ERROR} {_condense(item.get('message', 'error'))}"]

    if kind == "turn.failed":
        message = (event.get("error") or {}).get("message", "turn failed")
        return [f"  {ICON_TOOL_ERROR} {_condense(message)}"]

    if kind == "turn.completed":
        return [f"{ICON_FINISHED} worker finished"]

    return []


def find_turn_failure(stdout: str) -> str | None:
    """
    Scan a captured JSONL stream for a `turn.failed` event.

    Codex's clearest failure signal -- distinct from a normal completion even when the
    process itself still exits 0, which is why `run_worker` checks this ahead of the output
    file rather than trusting a zero exit code alone.
    """
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.failed":
            return str((event.get("error") or {}).get("message", "turn failed"))
    return None


#: Candidate wire names per TokenUsage field, tried in order.
#:
#: Deliberately tolerant, unlike the Claude/Grok tables. Those two emit the Anthropic
#: Messages API format, whose key names are pinned by a public API; Codex's `usage` payload
#: is known here only from this module's own docstring (`turn.completed.usage`) and has not
#: been checked against a captured stream. Trying several plausible spellings and returning
#: None on a total miss means a wrong guess degrades to "no data" -- which the scheduler and
#: dashboard already render as `-` -- rather than to a confidently wrong number, which is the
#: worse failure for something whose entire purpose is measurement.
#:
#: Confirm against a real `codex exec --json` stream and then narrow this table.
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cached_input_tokens", "cache_read_input_tokens", "cached_tokens"),
}


def _as_int(value: object) -> int | None:
    """A usage count, or None when absent or not a number (`bool` excluded — it is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _usage_from(payload: dict) -> TokenUsage | None:
    values = {}
    for field_name, wire_names in _USAGE_ALIASES.items():
        for wire_name in wire_names:
            count = _as_int(payload.get(wire_name))
            if count is not None:
                values[field_name] = count
                break
    return TokenUsage(**values) if values else None


def find_usage(stdout: str) -> TokenUsage | None:
    """
    Scan a captured JSONL stream for the turn's token usage, or None when it reports none.

    A scan rather than a field read, because `run_worker` never parses an envelope for
    Codex the way the other adapters do -- it takes the final message from the `-o` output
    file, so the usage event would otherwise go past unread. Shaped exactly like
    `find_turn_failure` for that reason.

    The last `turn.completed` wins: a stream carrying more than one reports the final state.
    """
    usage: TokenUsage | None = None
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed":
            continue
        # Both nestings are accepted for the same reason the key names are: the exact
        # envelope shape is documented, not verified.
        for payload in (event.get("usage"), (event.get("turn") or {}).get("usage")):
            if isinstance(payload, dict):
                found = _usage_from(payload)
                if found is not None:
                    usage = found
                    break
    return usage


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
    `turn.failed` event, a missing output file and an explicit `KILN-STATUS: blocked` all
    become a blocked `WorkerInvocation`, never a raised exception.

    `on_output` receives each rendered line (defaults to printing to the pane), so tests can
    assert on what an operator would see without capturing stdout.

    `debug_base` is accepted for signature parity with the other adapters (the scheduler
    dispatches to whichever backend a role picked without knowing which). Unused here: `codex
    exec` has no `--debug-file`/`--log-dir` equivalent, and reusing the ambient `CODEX_HOME`
    (see module docstring) means Codex already logs every invocation unconditionally to
    `$CODEX_HOME/logs_2.sqlite` regardless of any flag this adapter passes.
    """
    full_prompt = build_full_prompt(definition, prompt)
    output_fd, output_path = tempfile.mkstemp(prefix="kiln-codex-", suffix=".txt")
    os.close(output_fd)
    output_file = Path(output_path)

    command = build_command(prompt=full_prompt, output_file=output_file, model=model)
    resolved = shutil.which(command[0])
    if resolved:
        command[0] = resolved

    emit = on_output or _default_emit
    timed_out = threading.Event()

    try:
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
            return _blocked(f"could not launch codex: {exc}", "", is_error=True)

        def _abort() -> None:
            timed_out.set()
            process.kill()

        # A watchdog rather than a per-line deadline: a worker that hangs producing no output
        # at all would otherwise block on readline forever.
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
        # Read once, up front: every path below this point ends in a WorkerInvocation, and a
        # turn that failed or produced no message still burned tokens worth counting.
        tokens = find_usage(stdout)

        failure = find_turn_failure(stdout)
        if failure:
            log.error("worker %s reported a failed turn: %s", definition.name, failure)
            return _blocked(failure, stdout, is_error=True, tokens=tokens)

        if process.returncode != 0:
            detail = stderr.strip() or f"codex exited {process.returncode}"
            log.error("worker %s failed: %s", definition.name, detail)
            return _blocked(detail, stdout, is_error=True, tokens=tokens)

        text = output_file.read_text(encoding="utf-8").strip() if output_file.is_file() else ""
        if not text:
            detail = stderr.strip() or "codex produced no output message"
            log.error("worker %s produced no output message: %s", definition.name, detail)
            return _blocked(detail, stdout, is_error=True, tokens=tokens)

        result = parse_worker_report(text)
        log.info(
            "worker %s finished: status=%s sentinel=%s tokens=%s",
            definition.name, result.status, result.sentinel_found,
            tokens.total if tokens else "-",
        )
        return WorkerInvocation(result=result, raw_output=text, tokens=tokens)
    finally:
        output_file.unlink(missing_ok=True)
