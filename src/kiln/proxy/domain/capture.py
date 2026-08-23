"""
What the proxy keeps, and what it refuses to keep.

Every policy decision about captured traffic lives here rather than in the server, so it
can be tested without a socket and audited in one place. Three rules shape the module:

1. **Credentials never reach the store.** Not redacted after the fact, not written and
   deleted — never passed to a record in the first place. `redact_headers` is applied at
   the boundary and `SENSITIVE_HEADERS` is the single list.
2. **Bodies are opt-in and bounded.** A captured request body contains the entire source
   the agent read, in plaintext, in a directory symlinked into every worktree. Metadata-only
   is the default even once capture is switched on.
3. **This is not `messages.db`.** That file is live swarm state, queried by the dashboard,
   the inbox and the `kiln-db` MCP server, and is meant to stay small enough to open in a
   SQLite browser. Request bodies are orders of magnitude larger and get their own store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from kiln.scheduler.domain.models import TokenUsage

#: Headers that must never be written to the store, lower-cased for comparison.
#:
#: `authorization` and `x-api-key` are the obvious ones. `cookie`/`set-cookie` are here
#: because a session cookie is a bearer credential in every way that matters, and
#: `proxy-authorization` because this *is* a proxy and it would be a special kind of failure
#: to leak the credential for the hop we ourselves introduced.
#:
#: `chatgpt-account-id` and `x-codex-turn-metadata` were added after a live Codex capture:
#: neither is a bearer credential, but the first is a stable real-account identifier and the
#: second carries `installation_id` plus session/thread ids. A store that lives in the repo
#: tree should not be the place those accumulate, and nothing in Kiln reads them.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "chatgpt-account-id",
        "x-codex-turn-metadata",
    }
)

#: Placeholder written in place of a dropped value, so a reader can tell "this header was
#: present and withheld" from "this header was absent" -- the two mean different things
#: when debugging an auth failure through the kiln.proxy.
REDACTED = "<redacted>"

#: Bodies larger than this are truncated. A single Claude Code request routinely carries a
#: six-figure-token system prompt; keeping them whole would outgrow the repo it measures.
DEFAULT_BODY_LIMIT_BYTES = 256 * 1024

#: Appended to a truncated body so nobody measures a prompt against a clipped copy.
TRUNCATION_MARKER = "\n…[truncated by kiln proxy]"

#: Total stored body bytes before the oldest rows are degraded to metadata-only.
#:
#: Bodies are what actually grows: measured on a real store, 107.6MB across 676 requests was
#: 98.3% bodies, at roughly 160KB per request. Metadata is ~2.9KB a row, so a metadata-only
#: store would need some 370,000 requests to reach a gigabyte and never comes near this.
#: The budget exists for `--capture full`, which reaches it in about 1,600 requests.
DEFAULT_BODY_BUDGET_BYTES = 256 * 1024 * 1024

#: Writes between budget checks. The check is a full-table SUM over the body columns, which
#: is cheap but not free, and the budget is a ceiling rather than a precise line.
BODY_BUDGET_CHECK_EVERY = 100


class CaptureMode(StrEnum):
    """
    How much of each exchange is kept.

    `METADATA` is the default even after capture is enabled: sizes, model, timing and token
    counts answer "which role is expensive" without ever writing prompt text to disk.
    `FULL` is the second, deliberate opt-in needed to answer "why".
    """

    METADATA = "metadata"
    FULL = "full"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """
    Replace every sensitive header's value with `REDACTED`, preserving its presence.

    Case-insensitive on the header name, because HTTP header names are, and a proxy that
    only caught lower-case `authorization` would pass `Authorization` straight through.
    """
    return {
        name: (REDACTED if name.lower() in SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }


def capture_body(
    body: str | None, mode: CaptureMode, limit: int = DEFAULT_BODY_LIMIT_BYTES
) -> str | None:
    """
    The body as it should be stored: nothing in metadata mode, bounded text in full mode.

    Returns None rather than an empty string for "not captured", so a reader can tell a
    withheld body from a genuinely empty one.
    """
    if mode is not CaptureMode.FULL or body is None:
        return None
    if len(body) <= limit:
        return body
    return body[:limit] + TRUNCATION_MARKER


def extract_model(request_body: str | None) -> str | None:
    """
    The `model` field from an Anthropic request, or None when it cannot be read.

    Read from the request rather than the response because it is present even when the call
    fails, and a failed expensive call is exactly the one worth attributing to a model.
    """
    if not request_body:
        return None
    try:
        payload = json.loads(request_body)
    except (ValueError, TypeError):
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return str(model) if isinstance(model, str) else None


#: Top-level request sections worth measuring separately, mapped to their column names.
#:
#: These three are the whole optimization argument. Measured on a real cycle: `tools` was
#: 28-34KB per request, `system` (the generated worker instructions) 5-6KB, and `messages`
#: 30-92KB and growing with the conversation. Anyone reasoning about token spend needs the
#: split, because the intuitive target -- the worker instructions -- turns out to be ~5% of
#: a request while the conversation is 60-70%.
COMPOSITION_SECTIONS = {
    "tools": "tools_bytes",
    "system": "system_bytes",
    "messages": "messages_bytes",
}


def extract_composition(request_body: str | None) -> dict[str, int]:
    """
    Bytes per top-level section of an Anthropic request, or `{}` when unreadable.

    Computed here, at capture time, rather than by whatever wants to display it. The
    dashboard polls every couple of seconds and re-parsing hundreds of 100KB bodies on each
    frame would make it unusable.

    The second reason matters more: this works in `METADATA` mode, where no body is ever
    written to disk. The most useful analysis the proxy offers therefore costs nothing in
    stored prompt text -- you can measure composition without keeping anyone's source code.
    """
    if not request_body:
        return {}
    try:
        payload = json.loads(request_body)
    except (ValueError, TypeError):
        # A truncated capture is not JSON. Better no numbers than wrong ones.
        return {}
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("input"), list) and "messages" not in payload:
        return _responses_composition(payload)
    return {
        section: _section_bytes(payload[section])
        for section in COMPOSITION_SECTIONS
        if section in payload
    }


def _responses_composition(payload: dict) -> dict[str, int]:
    """
    The same three buckets, read out of an OpenAI Responses request.

    Codex talks to `/responses`, not `/v1/messages`, and the shape shares no key names with
    Anthropic's: there is no top-level `tools`, no `system` and no `messages`. Everything is
    one flat `input` array whose items are distinguished by `type` and `role` -- tool
    definitions arrive as an `additional_tools` item, instructions as `developer` messages,
    the conversation as `user`/`assistant` ones.

    They are mapped onto the existing columns rather than given their own, because the
    question the dashboard asks -- how much of this request is tools, instructions,
    conversation -- is the same question regardless of which vendor's spelling answers it.
    Measured on a real Codex request: 23.3KB tools, 25.0KB instructions, 2.9KB conversation.
    """
    sizes = {"tools": 0, "system": 0, "messages": 0}
    if isinstance(payload.get("tools"), list):
        sizes["tools"] += _section_bytes(payload["tools"])
    if payload.get("instructions"):
        sizes["system"] += _section_bytes(payload["instructions"])

    for item in payload["input"]:
        if not isinstance(item, dict):
            continue
        # Type before role: an `additional_tools` item also carries `role: developer`, and
        # counting 23KB of tool schemas as instructions would hide the largest section.
        if item.get("type") == "additional_tools":
            sizes["tools"] += _section_bytes(item)
        elif item.get("role") == "developer":
            sizes["system"] += _section_bytes(item)
        else:
            sizes["messages"] += _section_bytes(item)
    return {name: size for name, size in sizes.items() if size}


def _section_bytes(section: object) -> int:
    """
    Re-encode one section the way the client sent it, and measure it in bytes.

    Both arguments matter and both were wrong at first. `json.dumps` defaults to `", "` and
    `": "` separators while the client sends compact JSON, and it defaults to `ensure_ascii`,
    which expands every non-ASCII character into a six-byte escape. Together they inflated
    each section by ~1%, enough that tools + system + messages summed to *more* than the
    request they came from -- a share of 101% is a visible tell that the measurement, not the
    traffic, is off. Measured against real bodies the three sections now come to 99.8% of the
    request, the remainder being the small scalar keys (`model`, `max_tokens`, `stream`).
    """
    return len(json.dumps(section, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _usage_from(payload: dict) -> TokenUsage:
    """Anthropic's usage object -> TokenUsage. Absent fields stay zero."""

    def count(name: str) -> int:
        value = payload.get(name)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    return TokenUsage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_tokens=count("cache_read_input_tokens"),
        cache_creation_tokens=count("cache_creation_input_tokens"),
    )


def _usage_from_responses(payload: dict) -> TokenUsage:
    """
    OpenAI's Responses usage object -> TokenUsage.

    **The subtraction is the whole point.** Anthropic reports `input_tokens` as the *fresh*
    input with cache reads counted separately; OpenAI reports `input_tokens` as the total
    with `input_tokens_details.cached_tokens` as a subset of it. Storing OpenAI's number
    as-is would double-count every cached token and make a Codex role's cache hit rate --
    the dashboard column that matters most -- read as roughly half its real value.

    `cache_write_tokens` sits in the same details object and is nested the same way, so it is
    subtracted too. Its presence was a surprise -- the shape was written assuming this API
    had no cache-write concept, and one live Codex call showed
    `{"cached_tokens": 0, "cache_write_tokens": 0}` sitting there. Reading it costs nothing
    and not reading it would have silently zeroed a column for every Codex role.
    """

    def count(value: object) -> int:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    details = payload.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = count(details.get("cached_tokens"))
    written = count(details.get("cache_write_tokens"))
    total_input = count(payload.get("input_tokens"))
    return TokenUsage(
        input_tokens=max(total_input - cached - written, 0),
        output_tokens=count(payload.get("output_tokens")),
        cache_read_tokens=cached,
        cache_creation_tokens=written,
    )


def extract_usage(response_body: str | None) -> TokenUsage | None:
    """
    Token usage from a response, whether it arrived as one JSON object or as an SSE stream.

    Streaming is the normal case here -- Claude Code requests `stream: true` -- and usage
    arrives split across two events: `message_start` carries the input and cache counts,
    `message_delta` carries the running output count. They are merged, with the *last*
    `message_delta` winning, because it is cumulative rather than incremental.

    Returns None when the body reports no usage at all, matching the adapters' rule that
    "nothing was reported" and "zero" are different facts.
    """
    if not response_body:
        return None

    stripped = response_body.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            return None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return None
        return _usage_from_any(usage)

    return _usage_from_sse(response_body)


def _usage_from_any(usage: dict) -> TokenUsage:
    """
    Dispatch a usage object to the right vendor reader.

    `input_tokens_details` is the tell: only the Responses API nests its cache figure, and
    only there does `input_tokens` include the cached portion. A Responses reply with no
    cached tokens may omit the key entirely -- which is harmless, because with nothing to
    subtract both readers produce the same answer.
    """
    if "input_tokens_details" in usage or "output_tokens_details" in usage:
        return _usage_from_responses(usage)
    return _usage_from(usage)


def _usage_from_sse(body: str) -> TokenUsage | None:
    """
    Merge usage across an SSE stream, whichever vendor's events it carries.

    Delegates to `StreamingUsageTracker` rather than repeating its parser: the two used to
    be separate implementations of the same state machine, which meant teaching the proxy a
    second wire format would have meant changing it twice and getting it right twice.
    """
    tracker = StreamingUsageTracker()
    # The trailing newline flushes the tracker's partial-line buffer, which exists for
    # chunk boundaries and would otherwise swallow a final unterminated event.
    tracker.feed((body + "\n").encode("utf-8"))
    return tracker.usage


class StreamingUsageTracker:
    """
    Pulls usage out of an SSE stream chunk by chunk, in constant memory.

    The proxy must not buffer a response to read its usage: buffering is exactly what would
    destroy the live pane output that `--output-format stream-json` exists to provide. So
    usage is parsed as bytes go past — a rolling line buffer, `message_start` captured when
    it arrives at the head, `message_delta`'s cumulative `output_tokens` overwritten each
    time one appears.

    Constant memory also means an hour-long response costs the same as a short one, which
    matters because the expensive cycles are the long ones.

    Two wire formats arrive here. Anthropic splits usage across `message_start` (input and
    cache counts) and a running `message_delta` (cumulative output). The Responses API used
    by Codex sends it once, complete, on `response.completed`. Both are recognised by event
    name, so nothing has to know which backend a role runs before reading its stream.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._started: TokenUsage | None = None
        self._output_tokens = 0
        #: Set by a terminal Responses event, which reports everything at once and is
        #: therefore authoritative on its own rather than merged with anything.
        self._complete: TokenUsage | None = None

    def feed(self, chunk: bytes) -> None:
        """Consume one chunk. Partial trailing lines are held until completed."""
        self._pending += chunk.decode("utf-8", errors="replace")
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload_text = line[len("data:") :].strip()
        if not payload_text or payload_text == "[DONE]":
            return
        try:
            event = json.loads(payload_text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return

        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self._started = _usage_from(usage)
        elif kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
                self._output_tokens = int(usage["output_tokens"])
        elif isinstance(kind, str) and kind.startswith("response."):
            # Responses API. `response.completed` is the normal ending, but `.incomplete`
            # and `.failed` also carry usage -- and a turn that burned tokens and then
            # failed is exactly the one worth having a number for.
            response = event.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            if isinstance(usage, dict):
                self._complete = _usage_from_responses(usage)

    @property
    def usage(self) -> TokenUsage | None:
        """Usage so far, or None when the stream reported none."""
        if self._complete is not None:
            return self._complete
        if self._started is None and not self._output_tokens:
            return None
        base = self._started or TokenUsage()
        return TokenUsage(
            input_tokens=base.input_tokens,
            output_tokens=self._output_tokens or base.output_tokens,
            cache_read_tokens=base.cache_read_tokens,
            cache_creation_tokens=base.cache_creation_tokens,
        )


@dataclass
class TrafficRecord:
    """One request/response exchange, already redacted and size-bounded."""

    role: str | None
    method: str
    path: str
    status_code: int | None = None
    duration_ms: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    model: str | None = None
    tokens: TokenUsage | None = None
    #: Bytes per top-level request section — see `extract_composition`. Recorded even in
    #: metadata mode, where the bodies themselves are never stored.
    composition: dict[str, int] = field(default_factory=dict)
    request_body: str | None = None
    response_body: str | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        # Belt and braces: the server redacts at the boundary, but a record constructed
        # anywhere else must not be able to smuggle a credential into the store.
        self.request_headers = redact_headers(self.request_headers)
