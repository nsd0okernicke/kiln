"""Read-only SQLite projections over the scheduler message queue."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

from ...domain.models import MessageStatus, QueueMessage
from .queue_storage import connect

STATUS_QUEUED = MessageStatus.QUEUED.value
STATUS_DELIVERED = MessageStatus.DELIVERED.value
STATUS_PROCESSED = MessageStatus.PROCESSED.value


def _message(row: sqlite3.Row) -> QueueMessage:
    return cast(QueueMessage, dict(row))


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


def oldest_queued_by_role(db_path: str | Path, branch: str) -> dict[str, str]:
    """
    `created_at` of the oldest still-queued message per target role.

    Queue *depth* was already visible and is the weaker signal: one message that has sat
    unserved for an hour says something is dead downstream, while five that arrived a minute
    ago say the swarm is busy. Depth alone cannot tell those apart.

    Current values are UTC ISO timestamps. The dashboard parser remains compatible with
    naive-local values stored by older databases.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT target, MIN(created_at) AS oldest FROM messages "
            "WHERE branch=? AND status=? GROUP BY target",
            (branch, STATUS_QUEUED),
        )
        return {row["target"]: str(row["oldest"]) for row in cur.fetchall()}


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


def count_work_item_arrivals(db_path: str | Path, work_item: str, branch: str, target: str) -> int:
    """
    How many times this work item has been addressed to `target`.

    That is the honest unit for "how many cycles has this been round": a full loop hands the
    item to each role exactly once, so the count of messages a *single* role has received for
    one work item is the number of laps. Counting all messages for the item instead would mix
    lap length into the number and make the same ceiling mean different things in `full` (four
    scheduled roles) and `fix` (two).

    Counts every status, not just queued: a lap that has already been processed still
    happened, and only counting live messages would let a swarm loop forever.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE work_item=? AND branch=? AND target=?",
            (work_item, branch, target),
        )
        return int(cur.fetchone()[0])


def recent_messages(db_path: str | Path, branch: str, limit: int = 10) -> list[QueueMessage]:
    """The most recent messages on a branch, newest first -- the dashboard's activity feed."""
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, target, status, content, created_at FROM messages "
            "WHERE branch=? ORDER BY created_at DESC LIMIT ?",
            (branch, limit),
        )
        return [_message(row) for row in cur.fetchall()]


#: How many recent work-item messages the board reads in one poll.
#:
#: A window, not a history: the cockpit board needs the *latest* message per work item, and
#: a run producing more than this many handoffs has long since finished the items at the far
#: end. Kept modest because the cockpit re-reads it every couple of seconds and each row
#: carries a full handoff body.
WORK_ITEM_WINDOW = 120


def work_item_messages(
    db_path: str | Path, branch: str, limit: int = WORK_ITEM_WINDOW
) -> list[QueueMessage]:
    """
    Recent messages on a branch, newest first — the cockpit board's input.

    Grouping into cards is left to the cockpit application rather than done
    in SQL. "Latest row per group" in SQLite is either a bare-column-with-MAX trick, which
    picks arbitrarily among rows sharing a `created_at` second — two hops of one cycle can
    easily land in the same second — or a window function this module would then depend on.
    Ordering here and grouping in a pure function is deterministic, and puts the lane rules
    somewhere they can be tested without a database.

    `rowid` breaks the tie `created_at` cannot: it is monotonic in insert order, which is
    exactly the ordering a same-second pair of handoffs needs.

    **Messages with no work item are included**, and that is the whole reason this is not a
    `work_item IS NOT NULL` query. A human's intake hop legitimately has no name yet -- the
    specifier is what invents one -- so filtering NULLs here made every new request invisible
    on the board until the specifier's first cycle finished, which took eight minutes on the
    run that exposed it. Whether an unnamed row deserves a card is a display question, and it
    is answered by the cockpit board projection, where it can be tested without a database.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, target, status, content, created_at, work_item, error, "
            "started_at, finished_at "
            "FROM messages WHERE branch=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (branch, limit),
        )
        return [_message(row) for row in cur.fetchall()]


def pending_for_role(db_path: str | Path, branch: str, role: str) -> list[QueueMessage]:
    """
    Everything one role has not explicitly acknowledged — what Attention reads.

    `count_queued_by_role` answers how many; this answers which, which is what a human
    reviewing completed cycles actually needs. Inbox delivery marks a human-target message
    processed as soon as it is displayed, which is deliberately distinct from the operator
    acknowledging it in the kiln.cockpit. `acked_at` records that second event.
    """
    with closing(connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, target, status, content, created_at, work_item "
            "FROM messages WHERE branch=? AND target=? AND acked_at IS NULL "
            "AND status IN (?, ?, ?) "
            "ORDER BY created_at DESC",
            (branch, role, STATUS_QUEUED, STATUS_DELIVERED, STATUS_PROCESSED),
        )
        return [_message(row) for row in cur.fetchall()]
