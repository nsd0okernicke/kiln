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
- No dollar cost anywhere in the output -- only token usage (`turn.completed.usage`, now
  verified live; see `_USAGE_ALIASES`). `cost_usd` is left at its dataclass default (`0.0`),
  same rationale as the Copilot adapter.
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


#: Env var Kiln sets on a pane to route that role through the capture kiln.proxy.
#:
#: Codex has no base-URL environment variable of its own -- unlike Claude's
#: `ANTHROPIC_BASE_URL` -- so the override has to arrive as `-c` flags on the command line.
#: Kiln therefore carries the URL in its own variable and each Codex call translates it,
#: which keeps the transport mechanism uniform (pane environment, inherited by the one-shot
#: worker subprocess) while the CLI-specific spelling stays in the adapter, alongside every
#: other Codex accommodation.
PROXY_BASE_URL_ENV = "KILN_PROXY_BASE_URL"

#: Name of the synthetic provider the overrides define. Arbitrary, but it appears in Codex's
#: own startup banner as `provider: kiln`, which makes a routed pane obvious at a glance.
PROXY_PROVIDER = "kiln"


def proxy_config_args(base_url: str | None) -> list[str]:
    """
    The `-c` overrides that point one Codex call at the capture proxy, or nothing.

    Verified live: the ChatGPT OAuth token is attached even when the base URL is a local
    host, so a subscription user needs no API key for this to work. `wire_api = "responses"`
    matters -- Codex speaks the Responses API, and letting it default to the chat shape
    produces a stream neither side can parse.

    Defined as `-c` rather than in a config file because the one-shot worker call passes
    `--ignore-user-config`, so anything written to `config.toml` would be skipped.
    """
    if not base_url:
        return []
    provider = f"model_providers.{PROXY_PROVIDER}"
    return [
        "-c",
        f"model_provider={PROXY_PROVIDER}",
        "-c",
        f'{provider}.name="{PROXY_PROVIDER}"',
        "-c",
        f'{provider}.base_url="{base_url.rstrip("/")}"',
        "-c",
        f'{provider}.wire_api="responses"',
    ]


def build_command(
    *,
    prompt: str,
    output_file: str | Path,
    model: str = "",
    proxy_base_url: str | None = None,
) -> list[str]:
    """Construct the one-shot argv. Pure -- spawns nothing."""
    command = [
        "codex",
        "exec",
        prompt,
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "-o",
        str(output_file),
    ]
    if model:
        command += ["-m", model]
    return command + proxy_config_args(proxy_base_url)


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
        return _render_completed_item(event.get("item") or {})

    if kind == "turn.failed":
        message = (event.get("error") or {}).get("message", "turn failed")
        return [f"  {ICON_TOOL_ERROR} {_condense(message)}"]

    if kind == "turn.completed":
        return [f"{ICON_FINISHED} worker finished"]

    return []


def _render_completed_item(item: dict) -> list[str]:
    item_type = item.get("type")
    if item_type == "command_execution":
        return _render_command(item)
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
    return []


def _render_command(item: dict) -> list[str]:
    if item.get("exit_code") not in (0, None):
        output = item.get("aggregated_output", "")
        return [f"  {ICON_TOOL_ERROR} {_condense(output or 'command failed')}"]
    return [f"  {ICON_TOOL} {_condense(item.get('command', 'command'))}"]


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
#: **Confirmed against a real `codex exec --json` stream**, which reports:
#:
#:     {"input_tokens": 13781, "cached_input_tokens": 11008,
#:      "cache_write_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}
#:
#: The first spelling in each tuple is the verified one; the rest are kept as a cheap hedge
#: against a CLI rename, on the same principle as before -- a miss degrades to "no data",
#: which the dashboard renders as `-`, rather than to a confidently wrong number.
#:
#: `cache_write_input_tokens` was missing entirely until that capture, so every Codex cycle
#: silently reported zero cache writes.
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cached_input_tokens", "cache_read_input_tokens", "cached_tokens"),
    "cache_creation_tokens": ("cache_write_input_tokens", "cache_creation_input_tokens"),
}


def _as_int(value: object) -> int | None:
    """A usage count, or None when absent or not a number (`bool` excluded — it is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _usage_from(payload: dict) -> TokenUsage | None:
    """
    Codex's usage object -> TokenUsage, with the cached portion taken back out of the input.

    **The subtraction is not cosmetic.** Codex renames the OpenAI Responses API's usage
    field for field -- `input_tokens_details.cached_tokens` becomes `cached_input_tokens`,
    `input_tokens_details.cache_write_tokens` becomes `cache_write_input_tokens` -- and
    keeps its semantics: `input_tokens` is the *total*, of which the cached and written
    counts are subsets. Anthropic's `input_tokens` means the opposite, the fresh remainder.

    Storing Codex's number as-is under Anthropic's meaning double-counts every cached token:
    the run that produced the numbers above would have reported 24,789 input tokens instead
    of 13,781, and its cache hit rate at roughly half the truth. Verified by capturing the
    same turn twice -- once from the CLI stream, once off the wire through the proxy -- and
    confirming the two are the same object under different key names.
    """
    values = {}
    for field_name, wire_names in _USAGE_ALIASES.items():
        for wire_name in wire_names:
            count = _as_int(payload.get(wire_name))
            if count is not None:
                values[field_name] = count
                break
    if not values:
        return None
    values["input_tokens"] = max(
        values.get("input_tokens", 0)
        - values.get("cache_read_tokens", 0)
        - values.get("cache_creation_tokens", 0),
        0,
    )
    return TokenUsage(**values)


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


def _output_file() -> Path:
    output_fd, output_path = tempfile.mkstemp(prefix="kiln-codex-", suffix=".txt")
    os.close(output_fd)
    return Path(output_path)


def _worker_command(
    definition: WorkerDefinition, prompt: str, model: str, output: Path
) -> list[str]:
    command = build_command(
        prompt=build_full_prompt(definition, prompt),
        output_file=output,
        model=model,
        proxy_base_url=os.environ.get(PROXY_BASE_URL_ENV),
    )
    resolved = shutil.which(command[0])
    if resolved:
        command[0] = resolved
    return command


def _completed_process_invocation(
    definition: WorkerDefinition,
    process: subprocess.Popen,
    output_file: Path,
    stdout: str,
) -> WorkerInvocation:
    stderr = (process.stderr.read() if process.stderr else "") or ""
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
        definition.name,
        result.status,
        result.sentinel_found,
        tokens.total if tokens else "-",
    )
    return WorkerInvocation(result=result, raw_output=text, tokens=tokens)


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
    output_file = _output_file()
    command = _worker_command(definition, prompt, model, output_file)

    emit = on_output or _default_emit

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
                # A new session so the whole group can be signalled on timeout without
                # touching the scheduler's own (POSIX); accepted and ignored on Windows.
                start_new_session=True,
            )
        except OSError as exc:
            log.error("could not launch worker %s: %s", definition.name, exc)
            return _blocked(f"could not launch codex: {exc}", "", is_error=True)

        # Two limits: `timeout` for a worker that is slow, `idle_timeout` for one that
        # has stopped. Only the first existed, and it charged the full hour for a
        # worker that had already gone quiet.
        capture = capture_json_stream(
            process,
            timeout=timeout,
            idle_timeout=idle_timeout,
            render_event=render_event,
            emit=emit,
            watchdog_factory=Watchdog,
            terminate=terminate_tree,
        )
        stdout = capture.stdout
        if capture.timeout_reason:
            log.error("worker %s killed: %s", definition.name, capture.timeout_reason)
            return _blocked(capture.timeout_reason, stdout, timed_out=True)

        return _completed_process_invocation(definition, process, output_file, stdout)
    finally:
        output_file.unlink(missing_ok=True)
