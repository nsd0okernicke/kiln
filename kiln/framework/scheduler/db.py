"""
Message-queue data access for .kiln/messages.db.

Single source of truth for every SQL statement touching the queue. The MCP channel
server (kiln/framework/mcp-server/channel.py) and the scheduler both call in here, so the
queue's semantics are defined once instead of being restated in prose in
kiln/project/skills/kiln-handoff/SKILL.md and re-implemented per caller.

Every function takes `db_path` explicitly — there is no module-level connection — so
tests point these at a scratch file with the real schema instead of mocking SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_DELIVERED = "delivered"
STATUS_PROCESSING = "processing"
STATUS_PROCESSED = "processed"

#: Normal handoff priority. 0-9 is high (architect handoffs, critical tasks), 100+ is
#: informational — see constitution/workflow.md "Priority values".
DEFAULT_PRIORITY = 50

#: UTC stamp format used for delivered_at/processed_at, matching channel.py's original
#: statements. created_at deliberately uses localtime instead, matching the schema
#: default and kiln-handoff/SKILL.md, so handoff timestamps read naturally to a human
#: watching a manual cycle.
_UTC_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', 'utc')"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  sender TEXT NOT NULL,
  target TEXT NOT NULL,
  priority INTEGER DEFAULT 50,
  status TEXT DEFAULT 'queued',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
  delivered_at TEXT,
  acked_at TEXT,
  processed_at TEXT,
  error TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  work_item TEXT
)
"""

INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status)"

#: Groups every message belonging to one piece of work, newest last.
#:
#: `branch` cannot do this job: it holds the *base* branch, which every role on a swarm
#: shares, so grouping by it groups everything into one bucket. Without a real grouping key
#: nothing can answer "what did this feature cost" or "how many cycles has it been round",
#: and loop detection has nothing to count.
WORK_ITEM_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_work_item ON messages(work_item,created_at)"
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with row access by column name."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str | Path) -> None:
    """
    Create the messages table and index if absent, and enable WAL.

    Mirrors the CREATE half of bin/kiln.ps1's inline init script (bin/kiln.ps1:1690).
    NOTE: the one-time repair for legacy tables whose `created_at` was NOT NULL without a
    default is intentionally NOT ported here — kiln.ps1 still owns that migration. Port it
    before this function replaces that inline script, or the repair is silently lost.

    **This creates; it does not migrate.** `CREATE TABLE IF NOT EXISTS` leaves an existing
    table exactly as it is, so a database created before `work_item` existed keeps the old
    shape and every insert naming that column fails with "no such column". That is accepted
    deliberately while Kiln is used only for test projects: delete `.kiln/messages.db` and
    let it be recreated. Add an ordered-migrations step here before anyone runs a project
    they cannot throw away.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.execute(SCHEMA_SQL)
        conn.execute(INDEX_SQL)
        conn.execute(WORK_ITEM_INDEX_SQL)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()


def fetch_and_deliver(db_path: str | Path, role: str, branch: str) -> dict | None:
    """
    Return the next queued-or-delivered message for a role and mark it delivered.

    Re-delivering an already-`delivered` message is deliberate: it makes an agent that
    crashed between delivery and processing pick the message back up instead of stranding
    it. Returns None when the inbox is empty.
    """
    log.debug("polling DB for queued/delivered messages (role=%s branch=%s)", role, branch)
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, sender, priority, content, created_at
            FROM messages
            WHERE target = ? AND branch = ? AND status IN (?, ?)
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            (role, branch, STATUS_QUEUED, STATUS_DELIVERED),
        )
        row = cur.fetchone()
        if not row:
            log.debug("no queued/delivered messages found")
            return None

        log.info(
            "message found: id=%s sender=%s priority=%s created_at=%s",
            row["id"], row["sender"], row["priority"], row["created_at"],
        )
        log.debug("content preview: %s", row["content"][:120].replace("\n", " "))
        cur.execute(
            f"UPDATE messages SET status=?, delivered_at={_UTC_NOW} WHERE id=?",
            (STATUS_DELIVERED, row["id"]),
        )
        conn.commit()
        log.info("marked id=%s as delivered", row["id"])
        return dict(row)


def count_queued(db_path: str | Path, role: str, branch: str) -> int:
    """Number of messages still waiting in a role's inbox."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE target=? AND branch=? AND status=?",
            (role, branch, STATUS_QUEUED),
        )
        return int(cur.fetchone()[0])


def count_queued_by_role(db_path: str | Path, branch: str) -> dict[str, int]:
    """Queued-message count per target role, for the dashboard's queue-depth column."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT target, COUNT(*) AS n FROM messages WHERE branch=? AND status=? "
            "GROUP BY target",
            (branch, STATUS_QUEUED),
        )
        return {row["target"]: int(row["n"]) for row in cur.fetchall()}


def cycles_by_work_item(db_path: str | Path, branch: str) -> dict[str, int]:
    """
    Message count per work item, newest-first by first appearance.

    The question the column was added to answer: how many hops has one piece of work been
    through. A swarm looping on the same feature shows up here as a count that keeps
    climbing, which is what a max-cycles guard needs to read.

    Messages with no work item are excluded rather than grouped under a NULL key -- the
    intake hop is the only one that legitimately has none, and it is not a cycle.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT work_item, COUNT(*) AS n FROM messages "
            "WHERE branch=? AND work_item IS NOT NULL GROUP BY work_item "
            "ORDER BY MIN(created_at) DESC",
            (branch,),
        )
        return {row["work_item"]: int(row["n"]) for row in cur.fetchall()}


def recent_messages(db_path: str | Path, branch: str, limit: int = 10) -> list[dict]:
    """The most recent messages on a branch, newest first -- the dashboard's activity feed."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, target, status, content, created_at FROM messages "
            "WHERE branch=? ORDER BY created_at DESC LIMIT ?",
            (branch, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def mark_processing(db_path: str | Path, message_id: str) -> bool:
    """Flag a message as actively being worked. False when no such message exists."""
    return _set_status(db_path, message_id, STATUS_PROCESSING)


def mark_processed(db_path: str | Path, message_id: str) -> bool:
    """Flag a message as fully handled. False when no such message exists."""
    return _set_status(db_path, message_id, STATUS_PROCESSED, stamp_column="processed_at")


def _set_status(
    db_path: str | Path,
    message_id: str,
    status: str,
    stamp_column: str | None = None,
) -> bool:
    assignments = "status=?"
    if stamp_column:
        # stamp_column is a module-controlled literal, never caller input.
        assignments += f", {stamp_column}={_UTC_NOW}"

    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE messages SET {assignments} WHERE id=?", (status, message_id))
        conn.commit()
        changed = cur.rowcount > 0

    if changed:
        log.info("marked id=%s as %s", message_id, status)
    else:
        log.warning("no message found with id=%s", message_id)
    return changed


def insert_handoff(
    db_path: str | Path,
    sender: str,
    target: str,
    content: str,
    branch: str,
    priority: int = DEFAULT_PRIORITY,
    work_item: str | None = None,
) -> str:
    """
    Queue a handoff message and return its generated id.

    Codifies step 4 of kiln/project/skills/kiln-handoff/SKILL.md. `created_at` is left to
    the same `datetime('now','localtime')` expression that skill specifies.

    `work_item` is the specifier's stable `Handoff:` name, stored as a column rather than
    left as prose inside `content` so it can be grouped and counted. **NULL is legitimate
    for the intake message only** — the specifier is what invents the name, so the
    human -> specifier hop has none yet, and per-work-item accounting starts at the
    specifier's first outbound handoff. Every later message must carry one; anything
    counting cycles per work item cannot count a NULL.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO messages
              (sender, target, priority, status, content, created_at, branch, work_item)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)
            RETURNING id
            """,
            (sender, target, priority, STATUS_QUEUED, content, branch, work_item or None),
        )
        message_id = str(cur.fetchone()[0])
        conn.commit()

    log.info("queued handoff id=%s %s -> %s (branch=%s)", message_id, sender, target, branch)
    return message_id


def verify_queued(db_path: str | Path, sender: str, branch: str) -> str | None:
    """
    Return the id of the most recent queued message from `sender`, or None.

    Codifies step 5 of kiln-handoff/SKILL.md: an INSERT that silently failed leaves
    nothing to find here, which is the caller's signal to retry the insert.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM messages
            WHERE sender=? AND branch=? AND status=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sender, branch, STATUS_QUEUED),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None
