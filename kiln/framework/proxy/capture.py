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
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from scheduler.adapters import TokenUsage

log = logging.getLogger(__name__)

#: Headers that must never be written to the store, lower-cased for comparison.
#:
#: `authorization` and `x-api-key` are the obvious ones. `cookie`/`set-cookie` are here
#: because a session cookie is a bearer credential in every way that matters, and
#: `proxy-authorization` because this *is* a proxy and it would be a special kind of failure
#: to leak the credential for the hop we ourselves introduced.
SENSITIVE_HEADERS = frozenset(
    {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}
)

#: Placeholder written in place of a dropped value, so a reader can tell "this header was
#: present and withheld" from "this header was absent" -- the two mean different things
#: when debugging an auth failure through the proxy.
REDACTED = "<redacted>"

#: Bodies larger than this are truncated. A single Claude Code request routinely carries a
#: six-figure-token system prompt; keeping them whole would outgrow the repo it measures.
DEFAULT_BODY_LIMIT_BYTES = 256 * 1024

#: Appended to a truncated body so nobody measures a prompt against a clipped copy.
TRUNCATION_MARKER = "\n…[truncated by kiln proxy]"


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
    return {
        section: len(json.dumps(payload[section]))
        for section in COMPOSITION_SECTIONS
        if section in payload
    }


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
        return _usage_from(usage) if isinstance(usage, dict) else None

    return _usage_from_sse(response_body)


def _usage_from_sse(body: str) -> TokenUsage | None:
    """Merge usage across an SSE stream's `message_start` and `message_delta` events."""
    started: TokenUsage | None = None
    output_tokens = 0

    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload_text = line[len("data:"):].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            event = json.loads(payload_text)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                started = _usage_from(usage)
        elif kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
                # Cumulative, not incremental -- the last one is the total.
                output_tokens = int(usage["output_tokens"])

    if started is None and not output_tokens:
        return None
    base = started or TokenUsage()
    return TokenUsage(
        input_tokens=base.input_tokens,
        output_tokens=output_tokens or base.output_tokens,
        cache_read_tokens=base.cache_read_tokens,
        cache_creation_tokens=base.cache_creation_tokens,
    )


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
    """

    def __init__(self) -> None:
        self._pending = ""
        self._started: TokenUsage | None = None
        self._output_tokens = 0

    def feed(self, chunk: bytes) -> None:
        """Consume one chunk. Partial trailing lines are held until completed."""
        self._pending += chunk.decode("utf-8", errors="replace")
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload_text = line[len("data:"):].strip()
        if not payload_text or payload_text == "[DONE]":
            return
        try:
            event = json.loads(payload_text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return

        if event.get("type") == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self._started = _usage_from(usage)
        elif event.get("type") == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
                self._output_tokens = int(usage["output_tokens"])

    @property
    def usage(self) -> TokenUsage | None:
        """Usage so far, or None when the stream reported none."""
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


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS traffic (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  model TEXT,
  status_code INTEGER,
  duration_ms INTEGER,
  request_bytes INTEGER NOT NULL DEFAULT 0,
  response_bytes INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_creation_tokens INTEGER,
  tools_bytes INTEGER,
  system_bytes INTEGER,
  messages_bytes INTEGER,
  request_headers TEXT,
  request_body TEXT,
  response_body TEXT
)
"""

INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_traffic_role_ts ON traffic(role, ts)"

#: Columns added after the table shipped, applied to existing stores by `ensure_schema`.
#:
#: `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a store created before
#: these columns existed would silently keep the old shape and every insert naming them
#: would fail. Same trap `scheduler.db.ensure_schema` documents for the message queue.
MIGRATIONS = (
    ("tools_bytes", "ALTER TABLE traffic ADD COLUMN tools_bytes INTEGER"),
    ("system_bytes", "ALTER TABLE traffic ADD COLUMN system_bytes INTEGER"),
    ("messages_bytes", "ALTER TABLE traffic ADD COLUMN messages_bytes INTEGER"),
)


class TrafficStore:
    """
    SQLite-backed capture log, deliberately separate from `messages.db`.

    Opens a connection per write rather than holding one, matching `scheduler.db`'s own
    rule: the proxy is a long-lived process and a held handle would keep a WAL checkpoint
    pinned for the whole run.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def ensure_schema(self) -> None:
        """Create the table if absent, then bring an existing one up to date."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(SCHEMA_SQL)
            conn.execute(INDEX_SQL)
            conn.execute("PRAGMA journal_mode=WAL")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(traffic)")}
            for column, statement in MIGRATIONS:
                if column not in existing:
                    conn.execute(statement)
                    log.info("traffic store: added column %s", column)
            conn.commit()

    def record(self, entry: TrafficRecord) -> int:
        """Persist one exchange, returning its row id."""
        tokens = entry.tokens
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO traffic (
                  ts, role, method, path, model, status_code, duration_ms,
                  request_bytes, response_bytes,
                  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                  tools_bytes, system_bytes, messages_bytes,
                  request_headers, request_body, response_body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.ts, entry.role, entry.method, entry.path, entry.model,
                    entry.status_code, entry.duration_ms,
                    entry.request_bytes, entry.response_bytes,
                    tokens.input_tokens if tokens else None,
                    tokens.output_tokens if tokens else None,
                    tokens.cache_read_tokens if tokens else None,
                    tokens.cache_creation_tokens if tokens else None,
                    entry.composition.get("tools"),
                    entry.composition.get("system"),
                    entry.composition.get("messages"),
                    json.dumps(entry.request_headers) if entry.request_headers else None,
                    entry.request_body, entry.response_body,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def request_stats_by_role(self) -> dict[str, dict[str, int]]:
        """
        Per-role request count and request-body sizes — the prompt-weight signal.

        Request *bytes* rather than tokens on purpose: this is the number Phase A cannot
        produce. A role whose every call carries a 100KB payload is re-sending context, and
        the average and the maximum together say whether that is every call or one outlier.

        Returns `{}` when the store has no rows yet, so a caller can skip the section
        rather than render an empty table.
        """
        if not self.db_path.is_file():
            return {}
        with closing(sqlite3.connect(self.db_path)) as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT role, COUNT(*), AVG(request_bytes), MAX(request_bytes),
                           SUM(request_bytes),
                           AVG(tools_bytes), AVG(system_bytes), AVG(messages_bytes)
                    FROM traffic WHERE role IS NOT NULL GROUP BY role ORDER BY role
                    """
                ).fetchall()
            except sqlite3.DatabaseError:
                # A store written by an older/newer build, or a half-created file. The
                # dashboard must not die over an optional panel.
                return {}
        return {
            row[0]: {
                "requests": int(row[1]),
                "avg_bytes": int(row[2] or 0),
                "max_bytes": int(row[3] or 0),
                "total_bytes": int(row[4] or 0),
                # None (not 0) when nothing recorded it -- rows captured before these
                # columns existed, or bodies that could not be parsed. The dashboard shows
                # `-` rather than claiming a section was empty.
                "avg_tools": int(row[5]) if row[5] is not None else None,
                "avg_system": int(row[6]) if row[6] is not None else None,
                "avg_messages": int(row[7]) if row[7] is not None else None,
            }
            for row in rows
        }

    def totals_by_role(self) -> dict[str, TokenUsage]:
        """Token usage per role — the proxy's own answer to Phase A's dashboard columns."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT role,
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM(cache_creation_tokens), 0)
                FROM traffic WHERE role IS NOT NULL GROUP BY role
                """
            ).fetchall()
        return {
            row[0]: TokenUsage(
                input_tokens=row[1], output_tokens=row[2],
                cache_read_tokens=row[3], cache_creation_tokens=row[4],
            )
            for row in rows
        }
