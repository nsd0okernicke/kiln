#!/usr/bin/env python3
"""
Kiln MCP Server - Domain-level API with push notifications.

Provides domain tools (send_message, read_inbox, mark_delivered, mark_processed)
instead of raw SQL. When a new message arrives for an agent, all subscribed clients
receive a notifications/resources/updated notification immediately.

Usage:
    python kiln_db_server.py <path/to/messages.db> <role>
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
    Resource,
    ResourceTemplate,
    TextContent,
    ServerCapabilities,
)

# Global state
db_path: str = ""
subscriptions: dict[str, set[str]] = {}  # uri -> set of session_ids
last_message_ids: set[str] = set()  # Track already-notified messages
agent_role: str | None = None  # The role this server instance serves (extracted from db_path)
auto_subscribed: set[str] = set()  # Session IDs we've already auto-subscribed


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


@server.list_resources()
async def list_resources() -> list[ResourceTemplate]:
    """List available inbox resources."""
    # Return a template for dynamic inbox resources
    return [
        ResourceTemplate(
            uri_template="kiln://inbox/{role}",
            name="Agent Inbox",
            description="Queued messages for a specific agent role",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Read a resource (inbox for a role)."""
    role = get_role_from_uri(uri)
    if not role:
        raise ValueError(f"Invalid resource URI: {uri}")

    messages = get_queued_messages(role)
    return json.dumps({"role": role, "queued": messages})


@server.subscribe_resource()
async def subscribe_resource(uri: str, session_id: str) -> None:
    """Subscribe to a resource (inbox notifications)."""
    role = get_role_from_uri(uri)
    if not role:
        raise ValueError(f"Invalid resource URI: {uri}")

    if uri not in subscriptions:
        subscriptions[uri] = set()
    subscriptions[uri].add(session_id)


@server.unsubscribe_resource()
async def unsubscribe_resource(uri: str, session_id: str) -> None:
    """Unsubscribe from a resource."""
    if uri in subscriptions:
        subscriptions[uri].discard(session_id)
        if not subscriptions[uri]:
            del subscriptions[uri]


def ensure_agent_auto_subscribed() -> None:
    """Ensure the agent is auto-subscribed to their inbox."""
    global agent_role, auto_subscribed

    if agent_role is None or auto_subscribed:
        return

    # Use a sentinel session_id for auto-subscriptions
    # All clients will match this subscription
    auto_session = "__auto_subscribe__"
    uri = f"kiln://inbox/{agent_role}"
    if uri not in subscriptions:
        subscriptions[uri] = set()
    subscriptions[uri].add(auto_session)
    auto_subscribed.add(uri)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> str:
    """Execute domain-level tools for message handling."""
    # Ensure auto-subscription is set up
    ensure_agent_auto_subscribed()

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

            # Notify subscribers for this target
            uri = f"kiln://inbox/{target}"
            if uri in subscriptions:
                try:
                    await server.request_context.notify(
                        "notifications/resources/updated",
                        {"uri": uri},
                    )
                except Exception:
                    pass

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

    elif name == "subscribe_to_inbox":
        # Explicit subscription tool for agents to call on startup
        # This is a workaround since MCP's subscribe_resource is not directly callable by agents
        if not agent_role:
            return json.dumps({"error": "Server role not configured"})

        uri = f"kiln://inbox/{agent_role}"
        # Add a subscription entry (the MCP framework will handle the actual session)
        if uri not in subscriptions:
            subscriptions[uri] = set()

        # Add a marker that this role has been explicitly subscribed
        subscriptions[uri].add("__explicit_subscribe__")

        return json.dumps({
            "success": True,
            "message": f"Subscribed to {uri}",
            "uri": uri
        })

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
        {
            "name": "subscribe_to_inbox",
            "description": "Subscribe to your inbox to receive push notifications when messages arrive",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]


async def notification_loop():
    """Background loop that checks for new messages and notifies subscribers."""
    while True:
        try:
            await asyncio.sleep(0.25)  # Check every 250ms
            new_messages = check_new_messages()
            for role, messages in new_messages.items():
                uri = f"kiln://inbox/{role}"
                if uri in subscriptions:
                    try:
                        await server.request_context.notify(
                            "notifications/resources/updated",
                            {"uri": uri},
                        )
                    except Exception:
                        # Silently ignore notification errors
                        pass
        except Exception:
            # Continue loop even if notification fails
            pass


async def main():
    """Start the MCP server."""
    global db_path, agent_role

    if len(sys.argv) < 3:
        print("Usage: kiln_db_server.py <path/to/messages.db> <role>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    agent_role = sys.argv[2]

    # Verify database exists
    if not Path(db_path).exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[kiln-db] Database: {db_path}", file=sys.stderr)
    print(f"[kiln-db] Serving role: {agent_role}", file=sys.stderr)

    # Start notification loop in background
    asyncio.create_task(notification_loop())

    # Run the MCP server
    async with stdio_server() as streams:
        await server.run(
            streams[0], streams[1],
            InitializationOptions(
                server_name="kiln-db",
                server_version="1.0.0",
                capabilities=ServerCapabilities()
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
