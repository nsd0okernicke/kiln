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
from typing import cast

from .models import DEFAULT_PRIORITY, InboundMessage, MessageStatus, QueueMessage, can_transition
from .queue_queries import (
    WORK_ITEM_WINDOW as WORK_ITEM_WINDOW,
)
from .queue_queries import (
    count_queued as count_queued,
)
from .queue_queries import (
    count_queued_by_role as count_queued_by_role,
)
from .queue_queries import (
    count_work_item_arrivals as count_work_item_arrivals,
)
from .queue_queries import (
    cycles_by_work_item as cycles_by_work_item,
)
from .queue_queries import (
    oldest_queued_by_role as oldest_queued_by_role,
)
from .queue_queries import (
    pending_for_role as pending_for_role,
)
from .queue_queries import (
    recent_messages as recent_messages,
)
from .queue_queries import (
    work_item_messages as work_item_messages,
)
from .queue_storage import connect

log = logging.getLogger(__name__)


def _message(row: sqlite3.Row) -> QueueMessage:
    """Give SQLite's runtime mapping the queue contract known from each SELECT."""
    return cast(QueueMessage, dict(row))


def _inbound_message(row: sqlite3.Row) -> InboundMessage:
    """Type a row from the delivery SELECT, which always includes id and content."""
    return cast(InboundMessage, dict(row))

STATUS_QUEUED = MessageStatus.QUEUED.value
STATUS_DELIVERED = MessageStatus.DELIVERED.value
STATUS_PROCESSING = MessageStatus.PROCESSING.value
STATUS_PROCESSED = MessageStatus.PROCESSED.value
#: An escalated message: the cycle failed and a human was told. Deliberately *not* a state
#: `fetch_and_deliver` selects, so a failure is never silently re-served -- it comes back only
#: when someone runs `kiln retry`. Escalations used to be marked `processed`, which stopped
#: them wedging in `processing` but also made them indistinguishable from work that succeeded.
STATUS_FAILED = MessageStatus.FAILED.value

#: Normal handoff priority. 0-9 is high (architect handoffs, critical tasks), 100+ is
#: informational — see constitution/workflow.md "Priority values".
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


def fetch_and_deliver(db_path: str | Path, role: str, branch: str) -> InboundMessage | None:
    """
    Return the next queued-or-delivered message for a role and mark it delivered.

    Re-delivering an already-`delivered` message is deliberate: it makes an agent that
    crashed between delivery and processing pick the message back up instead of stranding
    it. Returns None when the inbox is empty.
    """
    return _fetch_and_deliver(db_path, role, branch, resumed_only=False)


def fetch_resume(db_path: str | Path, role: str, branch: str) -> InboundMessage | None:
    """
    As `fetch_and_deliver`, but only messages a human explicitly sent back.

    What a halted role polls. After the circuit breaker trips, the scheduler must ignore
    ordinary traffic -- it has already failed three cycles in a row and nothing has changed --
    while still noticing a `kiln retry`. `acked_at` is the difference: only `resume_failed`
    writes it, so it distinguishes "a human looked at this and sent it back" from every other
    queued message without inventing a second status to mean the same thing.
    """
    return _fetch_and_deliver(db_path, role, branch, resumed_only=True)


def _fetch_and_deliver(
    db_path: str | Path, role: str, branch: str, resumed_only: bool
) -> InboundMessage | None:
    kind = "resumed" if resumed_only else "queued/delivered"
    log.debug("polling DB for %s messages (role=%s branch=%s)", kind, role, branch)
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, sender, priority, content, created_at, acked_at
            FROM messages
            WHERE target = ? AND branch = ? AND status IN (?, ?)
            {"AND acked_at IS NOT NULL" if resumed_only else ""}
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            (role, branch, STATUS_QUEUED, STATUS_DELIVERED),
        )
        row = cur.fetchone()
        if not row:
            log.debug("no %s messages found", kind)
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
        return _inbound_message(row)


def recover_stale_processing(
    db_path: str | Path, role: str, branch: str
) -> list[QueueMessage]:
    """
    Reset this role's abandoned `processing` rows to `delivered`, returning what was reset.

    `fetch_and_deliver` re-serves `queued` and `delivered` rows but not `processing` ones, so
    a message flagged `processing` at the start of a cycle stays that way forever if the
    scheduler is killed mid-cycle -- `kiln --stop`, a closed pane, a crash. It is never
    re-served and never counted: `count_queued` and `count_queued_by_role` both filter on
    `queued`, so the dashboard's queue depth does not show it either. The work is silently
    lost, and stopping a swarm mid-cycle is routine.

    **No staleness heuristic is needed, so none is used.** Messages are addressed to a role,
    and exactly one scheduler process serves a given role's queue. At that role's own
    startup, any `processing` row for `(target=role, branch=branch)` is stale by definition:
    the only process that could have been working it is the one now starting. There is no
    live sibling to race, and therefore no timeout to tune. Scoping to `(role, branch)` is
    what makes that argument hold -- a table-wide reset would trample a *different* role's
    live cycle.

    Recovered rows re-enter through the existing `delivered` crash-recovery path rather than
    a new status, so delivery semantics are unchanged.

    **Caller must warn about replay.** A killed cycle may have left edited files, commits or
    a written `tmp/handoff-in.md` behind, so re-serving replays the cycle against a dirty
    worktree. That is survivable -- the squash anchor is recomputed each cycle and
    uncommitted work is staged into the next squash rather than lost -- but the worker may
    redo work it already did, which for a non-idempotent role is a real if mild hazard.
    Returning the rows instead of a count exists so the caller can log each one.

    (The inbox never marks `processing`, so this concerns scheduler roles only.)
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        # UPDATE ... RETURNING rather than SELECT-then-UPDATE: one statement, so there is no
        # window in which a row is reported recovered but not reset, or vice versa.
        cur.execute(
            """
            UPDATE messages SET status=?
            WHERE target=? AND branch=? AND status=?
            RETURNING id, sender, work_item, delivered_at
            """,
            (STATUS_DELIVERED, role, branch, STATUS_PROCESSING),
        )
        rows = [_message(row) for row in cur.fetchall()]
        conn.commit()
    return rows


def acknowledge_message(
    db_path: str | Path, message_id: str, role: str, branch: str
) -> QueueMessage | None:
    """Acknowledge one message addressed to `role`; return it, or None if out of scope."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE messages SET acked_at={_UTC_NOW}
            WHERE id=? AND target=? AND branch=? AND acked_at IS NULL
            RETURNING id, sender, target, branch, work_item, acked_at
            """,
            (message_id, role, branch),
        )
        row = cur.fetchone()
        conn.commit()
    return _message(row) if row else None


def mark_processing(db_path: str | Path, message_id: str) -> bool:
    """Flag a message as actively being worked. False when no such message exists."""
    return _set_status(db_path, message_id, STATUS_PROCESSING)


def mark_processed(db_path: str | Path, message_id: str) -> bool:
    """Flag a message as fully handled. False when no such message exists."""
    return _set_status(db_path, message_id, STATUS_PROCESSED, stamp_column="processed_at")


def mark_failed(db_path: str | Path, message_id: str, error: str) -> bool:
    """
    Flag a message as escalated, keeping the reason. False when no such message exists.

    Writes the `error` column, which was declared with the table and never written by any
    code. The reason is what makes the failure addressable afterwards -- "which message
    failed, and why" was previously answerable only by reading a scheduler log, if the pane
    still existed.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM messages WHERE id=?", (message_id,))
        row = cur.fetchone()
        if row is None:
            log.warning("no message found with id=%s", message_id)
            return False
        try:
            current = MessageStatus(row["status"])
        except ValueError:
            log.warning("message id=%s has unknown status %r", message_id, row["status"])
            return False
        if not can_transition(current, MessageStatus.FAILED):
            log.warning(
                "refusing invalid message transition %s -> failed for id=%s",
                current,
                message_id,
            )
            return False
        cur.execute(
            "UPDATE messages SET status=?, error=? WHERE id=?",
            (STATUS_FAILED, error, message_id),
        )
        conn.commit()
        changed = cur.rowcount > 0

    if changed:
        log.info("marked id=%s as %s: %s", message_id, STATUS_FAILED, error)
    else:
        log.warning("no message found with id=%s", message_id)
    return changed


def failed_messages(db_path: str | Path, branch: str) -> list[QueueMessage]:
    """Every escalated message on a branch, newest first -- what `kiln retry` lists."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, target, work_item, error, created_at FROM messages "
            "WHERE branch=? AND status=? ORDER BY created_at DESC",
            (branch, STATUS_FAILED),
        )
        return [_message(row) for row in cur.fetchall()]


def resume_failed(
    db_path: str | Path, message_id: str, content: str
) -> QueueMessage | None:
    """
    Put one failed message back in its own role's queue, with the human's guidance attached.

    The **same row** is re-queued rather than a new one inserted, so the work item, its lap
    count and its history stay attached to one identity -- a fresh row would look like new
    work to every guard that counts per work item.

    `acked_at` records the human's acknowledgement. That column, like `error`, was declared
    with the table and never written; this is evidently what it was for.

    Returns the updated row, or None when the id is unknown or the message is not failed.
    Refusing anything but a `failed` row is deliberate: re-queueing a message that is merely
    `processing` would hand a live scheduler a second copy of what it is already working on.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE messages
            SET status=?, content=?, acked_at={_UTC_NOW}, delivered_at=NULL
            WHERE id=? AND status=?
            RETURNING id, sender, target, branch, work_item, error, acked_at
            """,
            (STATUS_QUEUED, content, message_id, STATUS_FAILED),
        )
        row = cur.fetchone()
        conn.commit()

    if row is None:
        log.warning("no failed message with id=%s to resume", message_id)
        return None
    log.info("resumed id=%s for %s", message_id, row["target"])
    return _message(row)


def get_message(db_path: str | Path, message_id: str) -> QueueMessage | None:
    """One message by id, or None. For callers that need its content before rewriting it."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM messages WHERE id=?", (message_id,))
        row = cur.fetchone()
        return _message(row) if row else None


def _set_status(
    db_path: str | Path,
    message_id: str,
    status: str,
    stamp_column: str | None = None,
) -> bool:
    target = MessageStatus(status)
    assignments = "status=?"
    if stamp_column:
        # stamp_column is a module-controlled literal, never caller input.
        assignments += f", {stamp_column}={_UTC_NOW}"

    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM messages WHERE id=?", (message_id,))
        row = cur.fetchone()
        if row is None:
            log.warning("no message found with id=%s", message_id)
            return False
        try:
            current = MessageStatus(row["status"])
        except ValueError:
            log.warning("message id=%s has unknown status %r", message_id, row["status"])
            return False
        if not can_transition(current, target):
            log.warning(
                "refusing invalid message transition %s -> %s for id=%s",
                current,
                target,
                message_id,
            )
            return False
        cur.execute(f"UPDATE messages SET {assignments} WHERE id=?", (status, message_id))
        conn.commit()
        changed = cur.rowcount > 0

    if changed:
        log.info("marked id=%s as %s", message_id, status)
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


def message_exists(db_path: str | Path, message_id: str) -> bool:
    """
    True when a message with this id is in the queue, whatever state it has reached.

    This is how an insert is confirmed. The obvious-looking alternative -- "is there a queued
    message from me?" -- is what `verify_queued` did, and it is wrong in a way that took a
    live run to expose: the receiving scheduler polls every couple of seconds, so it can take
    the message and flip it out of `queued` **one second** after the insert. The check then
    finds nothing, the caller concludes its own insert failed silently, and inserts again.

    A duplicate handoff is not a harmless retry. Observed live: the specifier received the
    same request twice and ran two full cycles on it, ~650k tokens between them, and the coder
    was handed two specs for one work item.

    Identity is the thing that cannot be raced away. A row either exists or it does not, and
    no consumer can change that answer.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM messages WHERE id=? LIMIT 1", (message_id,))
        return cur.fetchone() is not None
