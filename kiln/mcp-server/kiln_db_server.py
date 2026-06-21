#!/usr/bin/env python3
"""
Kiln MCP Server - Domain-level API with SQLite + OS file-system push.

Provides domain tools (send_message, read_inbox, mark_delivered, mark_processed)
instead of raw SQL. Messages flow through a Channel that uses OS file-system
watching (watchdog) for instant push notifications across processes.

Usage:
    python kiln_db_server.py <path/to/messages.db>

Architecture:
  - send_message() calls Channel.send() → writes to SQLite
  - Watching agents subscribe via Channel.subscribe() → OS detects file change
  - Instant notification: no polling, no MCP push (OS handles the push)
"""

import asyncio
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Set up logging
log_file = Path(__file__).parent / "kiln_db_server.log"
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from mcp.server import Server, InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    ServerCapabilities,
    ToolsCapability,
    Tool,
)

from channel import Channel, Message

# Global state
db_path: str = ""
channel: Channel | None = None


def get_db() -> sqlite3.Connection:
    """Get a connection to the messages database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# Create the MCP server
server = Server("kiln-db")




@server.call_tool()
async def call_tool(name: str, arguments: dict) -> str:
    """Execute domain-level tools for message handling."""
    logger.info(f"Tool called: {name}")
    logger.debug(f"  Arguments: {arguments}")

    if name == "send_message":
        sender = arguments.get("sender")
        target = arguments.get("target")
        content = arguments.get("content")
        priority = arguments.get("priority", 50)
        branch = arguments.get("branch", "main")

        if not all([sender, target, content]):
            return json.dumps(
                {"error": "sender, target, and content are required"}
            )

        try:
            logger.info(f"send_message: {sender} -> {target} (priority={priority}, branch={branch})")
            logger.debug(f"  Content: {content[:100]}...")

            msg = Message(
                sender=sender,
                receiver=target,
                topic="kiln.handoff",
                payload={"content": content, "priority": priority, "branch": branch}
            )
            channel.send(msg)

            logger.info(f"  ✓ Message sent via channel (OS file-system push)")

            # OS file-system watch will notify listening agents instantly
            return json.dumps(
                {"success": True, "message_id": msg.id, "timestamp": msg.timestamp}
            )
        except Exception as e:
            logger.error(f"  ✗ Failed to send message: {e}")
            return json.dumps({"error": str(e)})

    elif name == "read_inbox":
        role = arguments.get("role")
        branch = arguments.get("branch", "main")

        if not role:
            logger.error("read_inbox: role is required")
            return json.dumps({"error": "role is required"})

        try:
            logger.info(f"read_inbox: role={role}, branch={branch}")
            conn = channel._conn()
            cursor = conn.execute(
                """
                SELECT id, sender, receiver, topic, payload, timestamp, status
                FROM messages
                WHERE receiver = ? AND branch = ? AND status = 'pending'
                ORDER BY timestamp ASC
                """,
                (role, branch),
            )
            rows = cursor.fetchall()
            messages = []
            for row in rows:
                msg_data = dict(row)
                # Flatten the payload for easier agent use
                if msg_data.get("payload"):
                    payload = json.loads(msg_data["payload"])
                    msg_data["content"] = payload.get("content", "")
                    msg_data["priority"] = payload.get("priority", 50)
                messages.append(msg_data)
            conn.close()
            logger.info(f"  Found {len(messages)} pending messages for {role}")
            return json.dumps({"inbox": messages})
        except Exception as e:
            logger.error(f"  ✗ read_inbox error: {e}")
            return json.dumps({"error": str(e)})

    elif name == "mark_delivered":
        message_id = arguments.get("message_id")

        if not message_id:
            logger.error("mark_delivered: message_id is required")
            return json.dumps({"error": "message_id is required"})

        try:
            logger.info(f"mark_delivered: message_id={message_id}")
            now = datetime.now().isoformat()
            conn = get_db()
            conn.execute(
                """
                UPDATE messages
                SET status = 'delivered', delivered_at = ?
                WHERE id = ?
                """,
                (now, message_id),
            )
            conn.commit()
            conn.close()
            logger.info(f"  ✓ Marked as delivered")
            return json.dumps({"success": True, "timestamp": now})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "mark_processed":
        message_id = arguments.get("message_id")

        if not message_id:
            logger.error("mark_processed: message_id is required")
            return json.dumps({"error": "message_id is required"})

        try:
            logger.info(f"mark_processed: message_id={message_id}")
            now = datetime.now().isoformat()
            conn = get_db()
            conn.execute(
                """
                UPDATE messages
                SET status = 'processed', processed_at = ?
                WHERE id = ?
                """,
                (now, message_id),
            )
            conn.commit()
            conn.close()
            logger.info(f"  ✓ Marked as processed")
            return json.dumps({"success": True, "timestamp": now})
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


@server.list_tools()
async def list_tools():
    """List available domain tools."""
    logger.info("list_tools() called - returning 4 tools")
    return [
        Tool(
            name="send_message",
            description="Send a message to another agent role",
            inputSchema={
                "type": "object",
                "properties": {
                    "sender": {
                        "type": "string",
                        "description": "Your role name (e.g., 'coder')",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target role name (e.g., 'refactorer')",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete handoff message content",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Message priority: 0-9 (high), 50 (normal), 100+ (low). Default: 50",
                        "default": 50,
                    },
                    "branch": {
                        "type": "string",
                        "description": "Git branch name. Default: 'main'",
                        "default": "main",
                    },
                },
                "required": ["sender", "target", "content"],
            },
        ),
        Tool(
            name="read_inbox",
            description="Read queued messages for your role",
            inputSchema={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Your role name (e.g., 'coder')",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Git branch name. Default: 'main'",
                        "default": "main",
                    },
                },
                "required": ["role"],
            },
        ),
        Tool(
            name="mark_delivered",
            description="Mark a message as delivered after reading it",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID returned by read_inbox",
                    }
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="mark_processed",
            description="Mark a message as processed after completing the work it describes",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID returned by read_inbox",
                    }
                },
                "required": ["message_id"],
            },
        ),
    ]




async def main():
    """Start the MCP server with Channel-based push notifications."""
    global db_path, channel

    logger.info("="*60)
    logger.info("Kiln MCP Server Starting")
    logger.info("="*60)

    if len(sys.argv) < 2:
        logger.error("Usage: kiln_db_server.py <path/to/messages.db>")
        sys.exit(1)

    db_path = sys.argv[1]
    logger.info(f"Database path: {db_path}")

    # Verify database exists
    if not Path(db_path).exists():
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    logger.info("Database exists and is accessible")

    # Initialize the Channel (SQLite + OS file-system watching)
    try:
        channel = Channel(db_path)
        logger.info("Channel initialized: SQLite + OS file-system watching ready")
        logger.info("  → Agents can subscribe for instant push notifications (no polling)")
    except Exception as e:
        logger.error(f"Failed to initialize Channel: {e}")
        sys.exit(1)

    # Run the MCP server
    async with stdio_server() as streams:
        await server.run(
            streams[0], streams[1],
            InitializationOptions(
                server_name="kiln-db",
                server_version="1.0.0",
                capabilities=ServerCapabilities(
                    tools=ToolsCapability()
                )
            )
        )




if __name__ == "__main__":
    asyncio.run(main())
