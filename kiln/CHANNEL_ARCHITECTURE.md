# Kiln Channel Architecture: Push-Based Agent Communication

## Overview

Kiln now uses **OS file-system watching** (via watchdog library) for instant push notifications between agents, eliminating the need for polling.

```
Agent A (coder)          Agent B (refactorer)
     |                          |
     | ch.send(msg)             |
     v                          |
  SQLite                        |
     |                          |
     | OS detects file change   |
     | (inotify/FSEvents)       |
     +------------------------->| ch.subscribe() callback fires
                                |
                                v
                           Agent B processes message
                                |
                                v
                           ch.send(response)
```

## Components

### 1. Channel Class (channel.py)

**Core**: SQLite + OS file-system watching via `watchdog` library.

**Key methods**:
- `ch.send(msg)` - Agent produces a message (writes to SQLite, triggers OS event)
- `ch.subscribe(role, callback)` - Agent subscribes to messages (blocks until arrival)
- `ch.ack(msg_id)` / `ch.nack(msg_id)` - Mark message as processed
- `ch.history()` - Inspect message log

**Why it works**:
1. SQLite WAL mode writes to `-wal` file first (transactional)
2. OS instantly detects file change
3. Watchdog observer fires callback in background thread
4. Callback deserializes and routes message to subscribers
5. **Zero polling, instant push**

### 2. MCP Server (kiln_db_server.py)

**Role**: Provides domain-level tools for agents to communicate.

**Tools**:
- `send_message(sender, target, content, priority, branch)` - Calls `ch.send()`
- `read_inbox(role, branch)` - Fallback polling (for non-subscribed agents)
- `mark_delivered(msg_id)` - Mark received
- `mark_processed(msg_id)` - Mark processed

**No notification_loop**: OS file-system watching replaces the old notification polling.

### 3. Agent Orchestrator (agent_orchestrator.py)

**Role**: Bridges Channel and Claude Code invocations.

**Flow**:
1. Runs as a background process for each agent role
2. Subscribes to the channel: `ch.subscribe(role, handle_message)`
3. When a message arrives, spawns Claude Code agent with the message
4. Claude Code processes and sends responses via MCP tools
5. Orchestrator acknowledges the message

**Usage**:
```bash
# Terminal 1: Start coder orchestrator
python kiln/mcp-server/agent_orchestrator.py \
    .kiln/messages.db \
    coder \
    'claude code --mcp-config .mcp.json'

# Terminal 2: Start refactorer orchestrator
python kiln/mcp-server/agent_orchestrator.py \
    .kiln/messages.db \
    refactorer \
    'claude code --mcp-config .mcp.json'

# Terminal 3: Run selftest (sends first message)
claude code kiln/roles/selftest.md --mcp-config .mcp.json
```

## How Agent Communication Works

### Scenario: coder sends to refactorer

**Step 1: Coder agent produces message**
```python
# Inside Claude Code (coder role)
send_message(
    sender="coder",
    target="refactorer",
    content="Here's the refactored code...",
    branch="main"
)
```

**Step 2: Message flows through Channel**
- `send_message()` tool calls MCP server
- MCP server calls `ch.send(msg)`
- Message inserted into SQLite
- SQLite WAL file modified
- OS detects change instantly

**Step 3: Refactorer orchestrator notified**
- Watchdog observer fires (milliseconds, no polling)
- `handle_message()` callback invoked in orchestrator process
- Orchestrator spawns Claude Code for refactorer role
- Message is passed to Claude Code via prompt

**Step 4: Refactorer processes message**
- Claude Code reads the message from the prompt
- Processes and decides next step
- Sends response via MCP: `send_message(sender="refactorer", target="next-agent", ...)`
- Cycle repeats

## Message Schema

```json
{
  "id": "uuid-string",
  "sender": "coder",
  "receiver": "refactorer",
  "topic": "kiln.handoff",
  "payload": {
    "content": "The actual message content",
    "priority": 50,
    "branch": "main"
  },
  "timestamp": "2026-06-21T10:30:45.123456",
  "status": "pending"  // or "done", "failed"
}
```

## Configuration

### .mcp.json

Each agent's `.mcp.json` points to the shared MCP server:

```json
{
  "mcpServers": {
    "kiln-db": {
      "command": "python",
      "args": ["kiln/mcp-server/kiln_db_server.py", ".kiln/messages.db"]
    }
  }
}
```

### Environment Variables

- `KILN_DB` - Path to messages.db (optional, defaults to `.kiln/messages.db`)

## Advantages Over Previous Approach

| Aspect | Old (File polling) | New (Channel) |
|--------|-------------------|---------------|
| **Notification** | Poll every 250ms | OS instant event |
| **CPU usage** | Constant polling | Idle until message |
| **Latency** | 0-250ms | <1ms |
| **Persistence** | SQLite + file-based notifications | SQLite only |
| **Scaling** | Polling every agent × N checks/sec | No per-agent overhead |
| **Reliability** | File-based edge cases | OS guarantees |

## Testing

### Manual Test

```bash
# Terminal 1: Start the MCP server
python kiln/mcp-server/kiln_db_server.py .kiln/messages.db

# Terminal 2: Start coder orchestrator
python kiln/mcp-server/agent_orchestrator.py .kiln/messages.db coder \
    'claude code --mcp-config .mcp.json'

# Terminal 3: Start refactorer orchestrator  
python kiln/mcp-server/agent_orchestrator.py .kiln/messages.db refactorer \
    'claude code --mcp-config .mcp.json'

# Terminal 4: Run selftest
claude code kiln/roles/selftest.md --mcp-config .mcp.json
```

### Expected Flow

1. Selftest sends message to coder
2. Coder orchestrator receives push notification instantly
3. Coder Claude Code invocation spawns
4. Coder processes and sends to refactorer
5. Refactorer orchestrator receives notification instantly
6. Refactorer Claude Code invocation spawns
7. ... message chain continues ...

## Troubleshooting

**Messages not received**: Check that agent orchestrators are running
- `ps aux | grep agent_orchestrator`
- Each role needs its own running orchestrator process

**Messages stuck "pending"**: Orchestrator may have crashed
- Check logs: `kiln/mcp-server/kiln_db_server.log`
- Restart the orchestrator for that role

**OS file-system watcher not firing**: Rare, but check:
- Watchdog library installed: `pip install watchdog`
- Database directory permissions (must be writable)
- On Linux/Mac: inotify/FSEvents working (system limits may apply)

## Future Enhancements

- [ ] HTTP mode for multi-machine orchestration (not just local IPC)
- [ ] Message priority queue with SLA enforcement
- [ ] Dead letter queue for failed messages
- [ ] Web dashboard for monitoring message flows
- [ ] Graceful shutdown with in-flight message handling
