#!/usr/bin/env python3
"""
Kiln MCP Server - Domain-level API with channel-based push notifications.

Provides domain tools (send_message, read_inbox, mark_delivered, mark_processed)
instead of raw SQL. Agents use the Claude Code --channels mechanism to receive
push notifications when new messages arrive via notifications/claude/channel.

Usage:
    python kiln_db_server.py <path/to/messages.db> <role>

Launch agents with: claude --channels server:kiln-db --mcp-config .mcp.json ...
"""

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

from mcp.server import Server, InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    ServerCapabilities,
    ToolsCapability,
)

# Global state
db_path: str = ""
last_message_ids: set[str] = set()  # Track already-notified messages for channel notifications


def get_db() -> sqlite3.Connection:
    """Get a connection to the messages database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_role_from_uri(uri: str) -> str | None:
    """Extract role name from resource URI (kiln://inbox/{role})."""
    if uri.startswith("kiln://inbox/"):
        return uri[len("kiln://inbox/") :]
    return None


def get_queued_messages(role: str) -> list[dict]:
    """Get all queued messages for a given role."""
    conn = get_db()
    try:
        cursor = conn.execute(
            """
            SELECT id, sender, priority, content, created_at
            FROM messages
            WHERE target = ? AND status = 'queued'
            ORDER BY priority ASC, created_at ASC
            """,
            (role,),
        )
        messages = [dict(row) for row in cursor.fetchall()]
        return messages
    finally:
        conn.close()


def check_new_messages() -> dict[str, list[dict]]:
    """
    Check for new queued messages and return grouped by target role.
    Returns dict of {role: [new_messages]}.
    """
    global last_message_ids

    conn = get_db()
    try:
        cursor = conn.execute(
            """
            SELECT id, target
            FROM messages
            WHERE status = 'queued'
            ORDER BY created_at ASC
            """
        )
        rows = cursor.fetchall()
        current_ids = {row["id"]: row["target"] for row in rows}

        # Find newly arrived messages (not in last_message_ids)
        new_ids = set(current_ids.keys()) - last_message_ids
        new_by_role: dict[str, list[dict]] = {}

        if new_ids:
            cursor = conn.execute(
                """
                SELECT id, target, sender, priority, content, created_at
                FROM messages
                WHERE id IN ({})
                ORDER BY priority ASC, created_at ASC
                """.format(",".join("?" * len(new_ids))),
                list(new_ids),
            )
            for row in cursor.fetchall():
                role = row["target"]
                if role not in new_by_role:
                    new_by_role[role] = []
                new_by_role[role].append(dict(row))

        last_message_ids = set(current_ids.keys())
        return new_by_role

    finally:
        conn.close()


# Create the MCP server
server = Server("kiln-db")




@server.call_tool()
async def call_tool(name: str, arguments: dict) -> str:
    """Execute domain-level tools for message handling."""

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
            message_id = str(uuid.uuid4())
            now = datetime.now().isoformat()
            conn = get_db()
            conn.execute(
                """
                INSERT INTO messages
                (id, sender, target, priority, status, content, created_at, branch)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (message_id, sender, target, priority, content, now, branch),
            )
            conn.commit()
            conn.close()

            # Notification will be pushed via notification_loop using --channels
            return json.dumps(
                {"success": True, "message_id": message_id, "timestamp": now}
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "read_inbox":
        role = arguments.get("role")
        branch = arguments.get("branch", "main")

        if not role:
            return json.dumps({"error": "role is required"})

        try:
            conn = get_db()
            cursor = conn.execute(
                """
                SELECT id, sender, priority, content, created_at
                FROM messages
                WHERE target = ? AND branch = ? AND status = 'queued'
                ORDER BY priority ASC, created_at ASC
                """,
                (role, branch),
            )
            messages = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return json.dumps({"inbox": messages})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "mark_delivered":
        message_id = arguments.get("message_id")

        if not message_id:
            return json.dumps({"error": "message_id is required"})

        try:
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
            return json.dumps({"success": True, "timestamp": now})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "mark_processed":
        message_id = arguments.get("message_id")

        if not message_id:
            return json.dumps({"error": "message_id is required"})

        try:
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
            return json.dumps({"success": True, "timestamp": now})
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


@server.list_tools()
async def list_tools():
    """List available domain tools."""
    return [
        {
            "name": "send_message",
            "description": "Send a message to another agent role",
            "inputSchema": {
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
        },
        {
            "name": "read_inbox",
            "description": "Read queued messages for your role",
            "inputSchema": {
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
        },
        {
            "name": "mark_delivered",
            "description": "Mark a message as delivered after reading it",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID returned by read_inbox",
                    }
                },
                "required": ["message_id"],
            },
        },
        {
            "name": "mark_processed",
            "description": "Mark a message as processed after completing the work it describes",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID returned by read_inbox",
                    }
                },
                "required": ["message_id"],
            },
        },
    ]


async def notification_loop():
    """Background loop that checks for new messages and pushes them via --channels."""
    while True:
        try:
            await asyncio.sleep(0.25)  # Check every 250ms
            new_messages = check_new_messages()
            for target_role, messages in new_messages.items():
                for msg in messages:
                    # Emit channel notification for each new message
                    # Agents filter by target role in the <channel> tag
                    try:
                        await server.request_context.notify(
                            "notifications/claude/channel",
                            {
                                "content": f"Message from {msg['sender']}: {msg['content']}",
                                "meta": {
                                    "target": target_role,
                                    "sender": msg["sender"],
                                    "message_id": msg["id"],
                                    "priority": msg["priority"],
                                    "created_at": msg["created_at"],
                                }
                            }
                        )
                    except Exception:
                        # Silently ignore notification errors
                        pass
        except Exception:
            # Continue loop even if notification fails
            pass


async def main():
    """Start the MCP server."""
    global db_path

    if len(sys.argv) < 2:
        print("Usage: kiln_db_server.py <path/to/messages.db>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]

    # Verify database exists
    if not Path(db_path).exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[kiln-db] Database: {db_path}", file=sys.stderr)
    print(f"[kiln-db] Ready to push messages via --channels", file=sys.stderr)

    # Start notification loop in background
    asyncio.create_task(notification_loop())

    # Run the MCP server
    async with stdio_server() as streams:
        await server.run(
            streams[0], streams[1],
            InitializationOptions(
                server_name="kiln-db",
                server_version="1.0.0",
                capabilities=ServerCapabilities(
                    tools=ToolsCapability(),
                    # Declare experimental claude/channel capability for push notifications
                    experimental={
                        "claude/channel": {}
                    }
                )
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
