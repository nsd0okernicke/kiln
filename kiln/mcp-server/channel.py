"""
Kiln Channel MCP server — role-scoped message receiver.

Exposes two tools to Claude:
  wait_for_message  – blocks until a queued message arrives for this role (or times out)
  get_channel_status – reports queue depth for debugging
"""

import asyncio
import logging
import os
import sqlite3
import sys
import time

from mcp.server.fastmcp import FastMCP

MY_ROLE = os.getenv("KILN_ROLE", "")
DB_PATH = os.getenv("KILN_DB_PATH", "")
POLL_INTERVAL = float(os.getenv("KILN_POLL_INTERVAL", "2.0"))
BRANCH = os.getenv("KILN_BRANCH", "main")
LOG_PATH = os.getenv("KILN_CHANNEL_LOG", "")

if not MY_ROLE or not DB_PATH:
    print("ERROR: KILN_ROLE and KILN_DB_PATH env vars required", file=sys.stderr)
    sys.exit(1)

# Logging: always stderr; also file if KILN_CHANNEL_LOG is set
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
if LOG_PATH:
    _handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [kiln-channel/%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("kiln-channel")
logging.getLogger("mcp").setLevel(logging.WARNING)

mcp = FastMCP("kiln-channel")


def _fetch_and_deliver() -> dict | None:
    """Return the next queued message and atomically mark it delivered, or None."""
    log.debug("polling DB for queued messages (role=%s branch=%s)", MY_ROLE, BRANCH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, sender, priority, content, created_at
            FROM messages
            WHERE target = ? AND branch = ? AND status = 'queued'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            (MY_ROLE, BRANCH),
        )
        row = cur.fetchone()
        if not row:
            log.debug("no queued messages found")
            return None
        log.info(
            "message found: id=%s sender=%s priority=%s created_at=%s",
            row["id"], row["sender"], row["priority"], row["created_at"],
        )
        log.debug("content preview: %s", row["content"][:120].replace("\n", " "))
        cur.execute(
            "UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        conn.commit()
        log.info("marked id=%s as delivered", row["id"])
        return dict(row)
    finally:
        conn.close()


@mcp.tool()
async def wait_for_message() -> dict:
    """
    Wait for the next message addressed to this role.

    Polls the Kiln message database every few seconds and blocks until a queued
    message is found and marked delivered.  Call this whenever you are ready to
    receive a handoff from another agent.  Call it again immediately if the
    session context is interrupted.
    """
    log.info("wait_for_message called (poll=%.1fs)", POLL_INTERVAL)
    poll_count = 0
    while True:
        try:
            msg = _fetch_and_deliver()
            if msg:
                log.info(
                    "returning message to caller: id=%s sender=%s priority=%s",
                    msg["id"], msg["sender"], msg["priority"],
                )
                return {"received": True, **msg}
        except sqlite3.OperationalError as exc:
            log.warning("DB locked (transient, will retry): %s", exc)
        poll_count += 1
        log.debug("poll #%d — no message yet", poll_count)
        await asyncio.sleep(POLL_INTERVAL)


@mcp.tool()
def get_channel_status() -> dict:
    """Return Channel configuration and the number of queued messages for this role."""
    log.debug("get_channel_status called")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE target=? AND branch=? AND status='queued'",
            (MY_ROLE, BRANCH),
        )
        queued = cur.fetchone()[0]
        conn.close()
        log.debug("queued=%d", queued)
        return {
            "role": MY_ROLE,
            "branch": BRANCH,
            "db_path": DB_PATH,
            "poll_interval_sec": POLL_INTERVAL,
            "queued_messages": queued,
            "status": "running",
        }
    except Exception as exc:
        log.error("get_channel_status failed: %s", exc)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    log.info(
        "starting — role=%s branch=%s poll=%.1fs db=%s log=%s",
        MY_ROLE, BRANCH, POLL_INTERVAL, DB_PATH, LOG_PATH or "(stderr only)",
    )
    mcp.run()
