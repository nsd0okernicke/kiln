"""
Kiln Channel MCP server — role-scoped message receiver.

Exposes two tools to Claude:
  wait_for_message  – blocks until a queued message arrives for this role (or times out)
  get_channel_status – reports queue depth for debugging

All SQL lives in scheduler/db.py so this server and the scheduler cannot drift apart on
queue semantics; this module is the MCP transport and response shaping around it.
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

# scheduler/ is a sibling package under kiln/framework. This file is always launched by
# absolute path (see the generated .mcp.json), never copied, so the framework root is a
# stable place to import from — but it is not on sys.path by default when running a
# script, hence the explicit insert.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# mcp 2.0 deleted `mcp.server.fastmcp` and renamed the class to `MCPServer`. The surface this
# module uses — `@server.tool()` and `.run()` defaulting to stdio — is identical in both, so a
# compatibility import supports either release.
#
# This is worth an import dance rather than a version pin because the server is launched by the
# *user's* interpreter (the generated .mcp.json calls bare `python`), so Kiln does not control
# which mcp is installed. Pinning would only move the failure to pip. Found live: an mcp 2.0.0
# install made kiln-channel fail to start at all, which silently degraded every wrapper-mode
# role to asking its human for help instead of receiving handoffs.
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ImportError:  # pragma: no cover - depends on the installed mcp release
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x

from kiln.scheduler.infrastructure.persistence import db

MY_ROLE = os.getenv("KILN_ROLE", "")
DB_PATH = os.getenv("KILN_DB_PATH", "")
POLL_INTERVAL = float(os.getenv("KILN_POLL_INTERVAL", "2.0"))
BRANCH = os.getenv("KILN_BRANCH", "main")
LOG_PATH = os.getenv("KILN_CHANNEL_LOG", "")

if not MY_ROLE or not DB_PATH:
    print("ERROR: KILN_ROLE and KILN_DB_PATH env vars required", file=sys.stderr)
    sys.exit(1)

# Logging: always stderr; also file if KILN_CHANNEL_LOG is set.
# Use open/close per write so Windows never holds an exclusive lock on the log file.
class _NonLockingFileHandler(logging.Handler):
    def __init__(self, path: str, encoding: str = "utf-8") -> None:
        super().__init__()
        self.path = path
        self.encoding = encoding

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with open(self.path, "a", encoding=self.encoding) as fh:
                fh.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)

_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
if LOG_PATH:
    _handlers.append(_NonLockingFileHandler(LOG_PATH))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [kiln-channel/%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("kiln-channel")
logging.getLogger("mcp").setLevel(logging.WARNING)

mcp = FastMCP("kiln-channel")


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
            msg = db.fetch_and_deliver(DB_PATH, MY_ROLE, BRANCH)
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
        queued = db.count_queued(DB_PATH, MY_ROLE, BRANCH)
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


@mcp.tool()
def mark_processing(message_id: str) -> dict:
    """Mark a message as being actively processed by the agent."""
    log.debug("mark_processing called for id=%s", message_id)
    try:
        if db.mark_processing(DB_PATH, message_id):
            return {"success": True, "message_id": message_id, "status": "processing"}
        return {"success": False, "error": f"message id {message_id} not found"}
    except Exception as exc:
        log.error("mark_processing failed: %s", exc)
        return {"success": False, "error": str(exc)}


@mcp.tool()
def mark_processed(message_id: str) -> dict:
    """Mark a message as fully processed by the agent."""
    log.debug("mark_processed called for id=%s", message_id)
    try:
        if db.mark_processed(DB_PATH, message_id):
            return {"success": True, "message_id": message_id, "status": "processed"}
        return {"success": False, "error": f"message id {message_id} not found"}
    except Exception as exc:
        log.error("mark_processed failed: %s", exc)
        return {"success": False, "error": str(exc)}


if __name__ == "__main__":
    log.info(
        # ASCII only: stderr goes to the console codepage (cp1252 here), not UTF-8, and
        # .mcp.json cannot set PYTHONIOENCODING for a server the agent CLI spawns.
        "starting - role=%s branch=%s poll=%.1fs db=%s log=%s",
        MY_ROLE, BRANCH, POLL_INTERVAL, DB_PATH, LOG_PATH or "(stderr only)",
    )
    mcp.run()
