"""
`--auto-approve-spec`: the human review gate, stood down for an unattended run.

Forwards the specifier's scenarios to the coder as though a human had clicked approve.
Nothing downstream is told it happened, so the row written here has to be indistinguishable
from a real approval — same sender, same work item, queued — apart from saying so in its
own text, which is the only place an operator reading the history afterwards can find out.

It polls rather than subscribes because two processes may start it (the human-in-the-loop
scheduler and the cockpit, each behind its own `--auto-approve-spec` flag) and neither owns
the queue. That is safe: the forward and the acknowledgement commit together, and the query
only sees unacknowledged rows, so whichever thread gets there first is the only one that
acts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Indirected so a test can stop the polling loop without waiting on a real sleep.
_sleep = time.sleep

#: How long between passes. Long enough not to contend with the schedulers for the database,
#: short enough that an unattended run does not visibly stall at the review gate.
POLL_SECONDS = 5.0

#: Grouping key for a spec that arrived without one. A NULL here would leave the story in no
#: bucket at all — no card, no cost line, nothing for loop detection to count.
UNNAMED_WORK_ITEM = "unknown"

_PENDING_SQL = (
    "SELECT id, work_item FROM messages "
    "WHERE sender='specifier' AND target='human-in-the-loop' "
    "AND acked_at IS NULL "
    "AND (status = 'processed' OR status = 'queued' OR status = 'delivered') "
    "ORDER BY created_at ASC"
)

_FORWARD_SQL = (
    "INSERT INTO messages (sender, target, priority, status, content, "
    "created_at, work_item, branch) "
    "VALUES (?, ?, 50, 'queued', ?, ?, ?, 'main')"
)

_ACKNOWLEDGE_SQL = "UPDATE messages SET acked_at=? WHERE id=? AND acked_at IS NULL"


def _approval_note(work_item: str) -> str:
    """The brief the coder receives, in the handoff envelope every other message uses."""
    return (
        f"Sender: human-in-the-loop\n"
        f"Handoff: {work_item}\n"
        f"Branch: main\n"
        f"Commit: \n\n"
        f"Auto-approved (--auto-approve-spec mode).\n"
        f"Next role: coder\n"
    )


def forward_pending_specs(db_path: str | Path) -> list[str]:
    """
    Forward every unreviewed spec to the coder. Returns the work items forwarded.

    One pass, so it is callable directly — the polling loop is `start_auto_approver`.
    Raises nothing of its own: the caller decides what a database it cannot read means.
    """
    forwarded: list[str] = []
    with closing(sqlite3.connect(str(db_path))) as conn:
        cur = conn.cursor()
        cur.execute(_PENDING_SQL)
        for msg_id, work_item in cur.fetchall():
            now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            named = work_item or UNNAMED_WORK_ITEM
            cur.execute(
                _FORWARD_SQL,
                ("human-in-the-loop", "coder", _approval_note(named), now, named),
            )
            # Same transaction as the insert: a forward that is not acknowledged would be
            # forwarded again on the next pass, and every pass after that.
            cur.execute(_ACKNOWLEDGE_SQL, (now, msg_id))
            conn.commit()
            log.info("auto-approved %s (message %s) -> coder", named, str(msg_id)[:8])
            forwarded.append(named)
    return forwarded


def start_auto_approver(
    db_path: str | Path, *, interval: float = POLL_SECONDS
) -> threading.Thread:
    """Poll for unreviewed specs forever on a daemon thread, forwarding each to the coder."""

    def _poll() -> None:
        while True:
            try:
                forward_pending_specs(db_path)
            except Exception as exc:  # a locked or half-written database is an expected transient
                log.warning("auto-approve: %s", exc)
            _sleep(interval)

    thread = threading.Thread(target=_poll, daemon=True, name="auto-approve")
    thread.start()
    log.info("auto-approve-spec: forwarding specifier -> coder without human review")
    return thread

