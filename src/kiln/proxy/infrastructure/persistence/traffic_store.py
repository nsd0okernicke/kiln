"""SQLite traffic-store adapter."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from kiln.scheduler.domain.models import TokenUsage

from ...domain.capture import (
    BODY_BUDGET_CHECK_EVERY,
    DEFAULT_BODY_BUDGET_BYTES,
    TrafficRecord,
)

log = logging.getLogger(__name__)

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

    def __init__(self, db_path: str | Path, body_budget: int = DEFAULT_BODY_BUDGET_BYTES) -> None:
        self.db_path = Path(db_path)
        self.body_budget = body_budget
        self._writes_since_check = 0

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
                    entry.ts,
                    entry.role,
                    entry.method,
                    entry.path,
                    entry.model,
                    entry.status_code,
                    entry.duration_ms,
                    entry.request_bytes,
                    entry.response_bytes,
                    tokens.input_tokens if tokens else None,
                    tokens.output_tokens if tokens else None,
                    tokens.cache_read_tokens if tokens else None,
                    tokens.cache_creation_tokens if tokens else None,
                    entry.composition.get("tools"),
                    entry.composition.get("system"),
                    entry.composition.get("messages"),
                    json.dumps(entry.request_headers) if entry.request_headers else None,
                    entry.request_body,
                    entry.response_body,
                ),
            )
            conn.commit()
            row_id = int(cursor.lastrowid or 0)

        self._writes_since_check += 1
        if self._writes_since_check >= BODY_BUDGET_CHECK_EVERY:
            self._writes_since_check = 0
            self.enforce_body_budget()
        return row_id

    def enforce_body_budget(self) -> int:
        """
        Drop the oldest stored bodies until the store is back inside its budget.

        Returns the number of rows degraded.

        **Bodies are cleared; rows are never deleted.** Composition and token usage are
        computed at capture time, so a row keeps its full analytical value once its bodies
        are gone -- the prompt-weight panel reads `tools_bytes`/`system_bytes`/
        `messages_bytes`, not the request text. Deleting rows instead would throw away the
        cheap history (~2.9KB each) to reclaim space the expensive columns were using.

        Metadata-mode stores never trip this: they write no bodies, so the sum stays zero.
        """
        if self.body_budget <= 0:
            return 0
        size = "length(coalesce(request_body,'')) + length(coalesce(response_body,''))"
        degraded = 0
        with closing(sqlite3.connect(self.db_path)) as conn:
            total = conn.execute(f"SELECT COALESCE(SUM({size}), 0) FROM traffic").fetchone()[0]
            if total <= self.body_budget:
                return 0
            # Oldest first, accumulating until enough has been freed. Done row by row rather
            # than with one bulk UPDATE because the cut-off depends on a running total.
            excess = total - self.body_budget
            freed = 0
            rows = conn.execute(
                f"SELECT id, {size} FROM traffic "
                "WHERE request_body IS NOT NULL OR response_body IS NOT NULL ORDER BY id"
            ).fetchall()
            for row_id, row_size in rows:
                if freed >= excess:
                    break
                conn.execute(
                    "UPDATE traffic SET request_body = NULL, response_body = NULL WHERE id = ?",
                    (row_id,),
                )
                freed += row_size
                degraded += 1
            conn.commit()
        if degraded:
            log.info(
                "traffic store over its %d-byte body budget; kept metadata and dropped "
                "bodies from the %d oldest rows",
                self.body_budget,
                degraded,
            )
        return degraded

    def request_stats_by_role(self, since: str | None = None) -> dict[str, dict[str, int]]:
        """
        Per-role request count and request-body sizes — the prompt-weight signal.

        Request *bytes* rather than tokens on purpose: this is the number Phase A cannot
        produce. A role whose every call carries a 100KB payload is re-sending context, and
        the average and the maximum together say whether that is every call or one outlier.

        `since` is an ISO-8601 UTC timestamp matching the `ts` column's own format; rows
        older than it are excluded. The store outlives any one run, and averaging across
        runs quietly blends configurations that are not comparable -- a role measured at
        220.8k was really 199k before a change and 118k after, and the mean describes
        neither. Lexicographic comparison is chronological for this format.

        Returns `{}` when nothing matches, so a caller can skip the section rather than
        render an empty table.
        """
        if not self.db_path.is_file():
            return {}
        with closing(sqlite3.connect(self.db_path)) as conn:
            try:
                # Which optional columns this store actually has, asked before selecting
                # them. Naming a missing column raises, and a blanket except would then
                # drop the *entire* panel over one absent field -- observed live against a
                # store written by a proxy that predated the composition columns. Degrading
                # per column keeps the request-size figures, which need nothing new.
                available = {row[1] for row in conn.execute("PRAGMA table_info(traffic)")}
                rows = _request_stats_rows(conn, available, since)
            except sqlite3.DatabaseError:
                # Not a usable store at all: a half-created file, or something that is not
                # SQLite. The dashboard must not die over an optional panel.
                return {}
        return {row[0]: _request_stats(row) for row in rows}

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
                input_tokens=row[1],
                output_tokens=row[2],
                cache_read_tokens=row[3],
                cache_creation_tokens=row[4],
            )
            for row in rows
        }


def _request_stats_rows(conn, available: set[str], since: str | None):
    optional = {
        name: (f"AVG({name})" if name in available else "NULL")
        for name in ("tools_bytes", "system_bytes", "messages_bytes")
    }
    window = "AND ts >= ?" if since else ""
    return conn.execute(
        f"""
        SELECT role, COUNT(*), AVG(request_bytes), MAX(request_bytes), SUM(request_bytes),
               {optional["tools_bytes"]}, {optional["system_bytes"]},
               {optional["messages_bytes"]}
        FROM traffic WHERE role IS NOT NULL {window}
        GROUP BY role ORDER BY role
        """,
        (since,) if since else (),
    ).fetchall()


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def _request_stats(row) -> dict[str, int | None]:
    return {
        "requests": int(row[1]),
        "avg_bytes": int(row[2] or 0),
        "max_bytes": int(row[3] or 0),
        "total_bytes": int(row[4] or 0),
        # None means the column was absent or no captured row recorded a composition.
        "avg_tools": _optional_int(row[5]),
        "avg_system": _optional_int(row[6]),
        "avg_messages": _optional_int(row[7]),
    }
