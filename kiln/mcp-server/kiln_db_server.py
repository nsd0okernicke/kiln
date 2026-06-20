#!/usr/bin/env python3
"""
Kiln MCP Server - Custom SQLite database server with push notifications.

Replaces mcp-sqlite with support for resource subscriptions on agent inboxes.
When a new message is written for an agent, all subscribed clients receive a
notifications/resources/updated notification immediately.

Usage:
    python kiln_db_server.py <path/to/messages.db>
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource,
    ResourceTemplate,
    TextContent,
)

# Global state
db_path: str = ""
subscriptions: dict[str, set[str]] = {}  # uri -> set of session_ids
last_message_ids: set[str] = set()  # Track already-notified messages


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


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> str:
    """Execute a tool (read_query or write_query)."""
    if name == "read_query":
        sql = arguments.get("query", "")
        if not sql:
            return json.dumps({"error": "query parameter required"})
        try:
            conn = get_db()
            cursor = conn.execute(sql)
            rows = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return json.dumps({"results": rows})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "write_query":
        sql = arguments.get("query", "")
        if not sql:
            return json.dumps({"error": "query parameter required"})
        try:
            conn = get_db()
            conn.execute(sql)
            conn.commit()
            conn.close()

            # Check for new messages and notify subscribers
            new_messages = check_new_messages()
            for role, messages in new_messages.items():
                uri = f"kiln://inbox/{role}"
                if uri in subscriptions:
                    for session_id in subscriptions[uri]:
                        await server.request_context.notify(
                            "notifications/resources/updated",
                            {"uri": uri},
                        )

            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


@server.list_tools()
async def list_tools():
    """List available tools."""
    return [
        {
            "name": "read_query",
            "description": "Execute a SELECT query and return results as JSON rows",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL SELECT query to execute",
                    }
                },
                "required": ["query"],
            },
        },
        {
            "name": "write_query",
            "description": "Execute an INSERT, UPDATE, or DELETE query",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL INSERT/UPDATE/DELETE query to execute",
                    }
                },
                "required": ["query"],
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
                    for session_id in subscriptions[uri]:
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
    global db_path

    if len(sys.argv) < 2:
        print("Usage: kiln_db_server.py <path/to/messages.db>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]

    # Verify database exists
    if not Path(db_path).exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # Start notification loop in background
    asyncio.create_task(notification_loop())

    # Run the MCP server
    async with stdio_server() as streams:
        await server.run(*streams)


if __name__ == "__main__":
    asyncio.run(main())
