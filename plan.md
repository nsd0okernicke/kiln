# Kiln Channel-Based Message Receiver — POC Plan

## Overview

**Goal:** Replace Kiln's polling-based message receiving with **proactive Channel notifications** to eliminate costly agent looping.

**Current state:** Agents manually poll `.kiln/messages.db` via `read_query` MCP tool to check for new messages.

**Desired state:** A Python Channel watches the database in the background and pushes notifications to Claude when matching messages arrive.

**Scope:** This is an **evolution of Kiln's existing messaging system**. We improve the **receiving side only**; message sending/queueing stays unchanged.

---

## Architecture

### Core Design: Hybrid MCP Approach

```
Kiln Startup
  ├─ Create .kiln/messages.db (existing)
  ├─ Spawn Channel subprocess (new)
  │   └─ Runs kiln/mcp-servers/db_channel_agent.py
  │       ├─ Connects to .kiln/messages.db
  │       ├─ Polls for messages WHERE target='<ROLE>' AND status='queued'
  │       └─ Pushes notifications to Claude when found
  │
  └─ Launch Claude agents with .mcp.json pointing to:
      ├─ kiln-db: mcp-sqlite (existing, for explicit SQL queries)
      └─ kiln-channel: db_channel_agent.py (new, for notifications)
```

### Data Flow

1. **Message Queued** (existing)
   - Agent A sends message: `INSERT INTO messages (target='coder', status='queued', ...)`

2. **Channel Detects** (new)
   - Channel polls every 5s: `SELECT * FROM messages WHERE target='coder' AND status='queued'`
   - Channel atomically marks as delivered: `UPDATE messages SET status='delivered', delivered_at=now() WHERE id=?`

3. **Claude Notified** (new)
   - Channel pushes: `mcp.notification("notifications/claude/channel", {...full message row...})`
   - Claude sees formatted notification in context

4. **Claude Processes** (existing + new instructions)
   - Claude reads notification, acts per its role instructions
   - When done, uses existing `send_message` tool to queue message for next agent

---

## Message Schema & Querying

**Existing Kiln Schema** (from `.kiln/messages.db`):

```sql
CREATE TABLE messages (
  id TEXT PRIMARY KEY,              -- Unique message ID
  sender TEXT NOT NULL,             -- Role that sent this message
  target TEXT NOT NULL,             -- Role this message is for
  priority INT DEFAULT 50,          -- 0-9: high, 50: normal, 100+: low
  status TEXT DEFAULT 'queued',     -- queued, delivered, processed
  content TEXT NOT NULL,            -- Message body (handoff text)
  created_at TEXT NOT NULL,         -- When message was created
  delivered_at TEXT,                -- When marked delivered
  acked_at TEXT,                    -- (Future) When acknowledged
  processed_at TEXT,                -- (Future) When fully processed
  error TEXT,                       -- (Future) Error if any
  branch TEXT DEFAULT 'main'        -- Git branch this message is for
)

CREATE INDEX idx_target_branch_status ON messages(target, branch, status)
```

**Receiving Filter** (what Channel watches):

```sql
SELECT id, sender, priority, content, created_at
FROM messages
WHERE target = ? AND branch = ? AND status = 'queued'
ORDER BY priority ASC, created_at ASC
LIMIT 1
```

The index `idx_target_branch_status` is **perfect** for this query.

---

## Channel Implementation

### File Structure

```
kiln/
  mcp-servers/
    db_channel_agent.py           -- Main Channel script
    requirements.txt              -- mcp, pydantic
```

### Channel Responsibility

- **Spawn time:** Kiln starts it as a subprocess at launch
- **Config:** Receives via environment variables per role
- **Loop:** Polls database every 5 seconds
- **Action:** When matching message found:
  1. Mark status = 'delivered' (atomic)
  2. Push notification to Claude
- **Resilience:** If crashed, status='queued' means message will be re-detected on restart

### Channel Does NOT Do

- Claude doesn't need extra tools for messaging (uses existing `send_message`)
- Channel doesn't track "processed" status (POC scope: queued → delivered only)
- Channel isn't responsible for routing logic (that's Claude's role instructions)

### Sample Implementation (`kiln/mcp-servers/db_channel_agent.py`)

```python
import asyncio
import os
import sqlite3
import json
import sys
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# FastMCP server for this role's Channel
mcp = FastMCP("kiln-channel")

# Config via environment (set by Kiln at spawn time)
MY_ROLE = os.getenv("KILN_ROLE")
DB_PATH = os.getenv("KILN_DB_PATH")
POLL_INTERVAL = float(os.getenv("KILN_POLL_INTERVAL", "5.0"))
BRANCH = os.getenv("KILN_BRANCH", "main")

if not MY_ROLE or not DB_PATH:
    print("ERROR: KILN_ROLE and KILN_DB_PATH env vars required", file=sys.stderr)
    sys.exit(1)

# Track what we've already delivered to avoid double-sends
last_delivered_id = None


async def poll_messages():
    """Poll database every POLL_INTERVAL seconds for new queued messages."""
    global last_delivered_id
    
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Find first queued message for this role
            cursor.execute("""
                SELECT id, sender, priority, content, created_at
                FROM messages
                WHERE target = ? AND branch = ? AND status = 'queued'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """, (MY_ROLE, BRANCH))
            
            row = cursor.fetchone()
            
            if row:
                message_id = row["id"]
                
                # Atomically mark as delivered before notifying
                cursor.execute("""
                    UPDATE messages
                    SET status = 'delivered', delivered_at = datetime('now')
                    WHERE id = ?
                """, (message_id,))
                conn.commit()
                
                # Build notification payload
                payload = {
                    "id": message_id,
                    "sender": row["sender"],
                    "priority": row["priority"],
                    "content": row["content"],
                    "created_at": row["created_at"]
                }
                
                # Push to Claude
                print(f"[{MY_ROLE}] ✓ Pushing message from {row['sender']} (priority {row['priority']})", 
                      file=sys.stderr)
                await mcp.notification("notifications/claude/channel", {
                    "title": f"MESSAGE FROM {row['sender'].upper()}",
                    "body": json.dumps(payload)
                })
                
                last_delivered_id = message_id
            
            conn.close()
            
        except sqlite3.OperationalError as e:
            # DB locked or missing (transient, log and retry)
            print(f"[{MY_ROLE}] ⚠ Database error (will retry): {e}", file=sys.stderr)
        except Exception as e:
            # Unexpected error (log, don't crash)
            print(f"[{MY_ROLE}] ✗ Unexpected error: {e}", file=sys.stderr)
        
        await asyncio.sleep(POLL_INTERVAL)


@mcp.tool()
def get_channel_status() -> dict:
    """Debug tool: report Channel status."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE target=? AND status='queued'",
            (MY_ROLE,)
        )
        queued_count = cursor.fetchone()[0]
        conn.close()
        
        return {
            "role": MY_ROLE,
            "db_path": DB_PATH,
            "branch": BRANCH,
            "poll_interval_sec": POLL_INTERVAL,
            "queued_messages": queued_count,
            "status": "✓ Running" if queued_count >= 0 else "✗ Error"
        }
    except Exception as e:
        return {"status": "✗ Error", "error": str(e)}


async def main():
    """Start Channel loop."""
    print(f"[{MY_ROLE}] Channel starting (db={DB_PATH}, branch={BRANCH}, poll={POLL_INTERVAL}s)", 
          file=sys.stderr)
    
    # Start polling loop in background
    poll_task = asyncio.create_task(poll_messages())
    
    # Run MCP server
    async with mcp:
        await poll_task


if __name__ == "__main__":
    asyncio.run(main())
```

---

## MCP Registration & Notification Format

### `.mcp.json` Layout (per agent)

```json
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "/path/to/.kiln/messages.db"]
    },
    "kiln-channel": {
      "command": "python",
      "args": ["kiln/mcp-servers/db_channel_agent.py"]
    }
  }
}
```

**Note:** Kiln generates this dynamically at startup, injecting `DB_PATH`, `KILN_ROLE`, etc. via environment variables.

### Notification Appearance to Claude

When a message arrives, Claude sees this in its context:

```
═══════════════════════════════════════════════════════════════
📨 MESSAGE FROM SPECIFIER
═══════════════════════════════════════════════════════════════
{
  "id": "20260619143522-abc123def",
  "sender": "specifier",
  "priority": 50,
  "content": "Re-read your role and constitution.\nSender: specifier\n...",
  "created_at": "2026-06-19 14:35:22"
}
═══════════════════════════════════════════════════════════════
```

Claude's role instructions tell it how to handle this (e.g., "When you see a MESSAGE section, treat it as a handoff and respond per your role").

---

## Error Handling

### Startup Errors (Fail Fast)

- **Missing `KILN_ROLE` or `KILN_DB_PATH`** → Exit with error message to stderr
- **Missing database file** → Exit with error (Kiln should have created it)
- **Permission denied on database** → Exit with error

**Why:** Config errors must be caught immediately. Kiln doesn't proceed if Channel can't start.

### Transient Errors (Log & Retry)

- **Database locked** → Log warning, sleep POLL_INTERVAL, retry
- **Notification delivery failed** → Log warning, continue polling (notification is ephemeral)
- **Connection timeout** → Log, retry next cycle

**Logging:** Print to stderr with role prefix: `[coder] ✓ Pushed message` or `[coder] ⚠ DB locked`

### Status Filter as Safety Net

If Channel crashes mid-cycle:
- It already marked the message `status='delivered'` (atomic before notification)
- On restart, query only finds `status='queued'` messages
- Won't re-notify Claude about old delivered messages
- **No state file needed**

---

## POC Scope & Testing Strategy

### MVP: Queued → Delivered Only

**Current:** `queued`, `delivered`, `processed` columns exist but we ignore `processed`.

**POC focus:** Just get message detection and notification working.
- ✓ Channel detects queued message
- ✓ Channel marks delivered
- ✓ Claude receives notification
- ✓ Claude responds using existing tools

**Future (Phase 2):** Track full `processed` lifecycle, add acknowledgment.

### Testing: Hybrid Approach

**Phase 1: Manual Single-Message Test**
1. Start one agent (coder) with Channel running
2. Manually insert test message into DB: `INSERT INTO messages (target='coder', ...)`
3. Verify Channel detects it (check stderr logs)
4. Verify Claude receives notification (check Claude context)
5. ✓ Confirm flow works end-to-end

**Phase 2: Selftest Chain**
1. Launch all 4 agents (specifier, coder, refactorer, architect) with Channels
2. Run existing selftest handoff prompt in specifier
3. Each agent's Channel detects message, pushes notification
4. Agent processes and forwards to next agent
5. ✓ Full chain validation (like current selftest but via Channels instead of polling)

---

## Integration with Kiln

### Kiln Startup Changes

**Current flow:**
```
kiln.ps1 / kiln.sh
  ├─ Create worktrees
  ├─ Generate CLAUDE.md
  ├─ Initialize .kiln/messages.db
  └─ Launch agents with .mcp.json (mcp-sqlite only)
```

**New flow:**
```
kiln.ps1 / kiln.sh
  ├─ Create worktrees
  ├─ Generate CLAUDE.md
  ├─ Initialize .kiln/messages.db
  ├─ [NEW] Copy db_channel_agent.py to .kiln/mcp-servers/
  ├─ [NEW] Spawn 4 Channel subprocesses (one per role, with env vars)
  └─ Launch agents with .mcp.json (mcp-sqlite + kiln-channel)
```

### Agent Instruction Changes

CLAUDE.md will include guidance on handling message notifications:

```markdown
### Receiving Messages (Automated via Channel)

When you see a **MESSAGE** section in your context, it's a handoff from another agent.
- Read the message content (in the `content` field)
- Apply your role instructions to the task
- When done, send a response using the existing `send_message` tool

The Channel automatically detects incoming messages and pushes them to you.
You do NOT need to manually check your inbox anymore.
```

---

## Architectural Decision Tree

### 1. Scope: What are we changing?

- **[A] Replace sending** (rip out current messaging)
- **[B] Parallel system** (new channel system alongside current messaging)
- **[C] Improve receiving only** ✓ **CHOSEN**
  - Keep message sending (mature, tested)
  - Replace polling loop with proactive notifications

**Why C:** Sending works; focus on the pain point (polling).

---

### 2. Who spawns the Channel?

- **[A] Claude spawns it** (Claude Code starts subprocess)
- **[B] Kiln spawns it** ✓ **CHOSEN (PRIMARY)**
  - Spawned at orchestration startup
  - Tied to agent lifecycle
  - Alternatives documented below if we hit blockers
- **[C] Manual** (user starts subprocess)
- **[D] Auto-discovery** (Claude finds it via registration)

**Why B:** Kiln orchestrates the whole swarm; Channel is part of setup.

**Fallback:** If Channel subprocess lifecycle is hard to manage from Kiln, try **(A)** — each Claude agent spawns its own Channel on startup.

---

### 3. Channel registration in MCP

- **[A] Static .mcp.json** (hardcoded paths, no runtime injection)
- **[B] Dynamic injection** ✓ **CHOSEN**
  - Kiln generates .mcp.json per agent at startup
  - Injects KILN_ROLE, DB_PATH, BRANCH via environment variables
- **[C] Config file discovery** (Channel finds config in standard location)

**Why B:** Clean separation; Kiln handles config.

---

### 4. Message filter

- **[A] Just `target`** (any message for my role)
- **[B] `target + status='queued'`** ✓ **CHOSEN**
  - Only watch for unprocessed messages
  - Status acts as state machine guard
- **[C] `target + (status='queued' OR status='delivered')` ** (safety belt)

**Why B:** Clean, simple, indexes exist.

---

### 5. Status lifecycle (POC scope)

- **[A] `queued` only** (no status updates)
- **[B] `queued → delivered`** ✓ **CHOSEN (POC)**
  - Channel marks delivered atomically
  - Skip `processed` for now (Phase 2)
- **[C] Full `queued → delivered → processed`** (future)

**Why B:** Minimal change for POC; gives observability without over-engineering.

---

### 6. Notification payload

- **[A] Minimal** (just message ID and preview)
- **[B] Full message row** ✓ **CHOSEN**
  - id, sender, priority, content, created_at
  - Claude has everything it needs
- **[C] Rich metadata** (add tracing, routing info)

**Why B:** Simple, complete, no over-engineering.

---

### 7. Claude's handling

- **[A] Manual** (notification appears, user prompts Claude to respond)
- **[B] Automatic per role instructions** ✓ **CHOSEN**
  - Role instructions tell Claude: "When you see a MESSAGE section, ..."
  - Claude processes automatically
- **[C] Hardcoded behavior** (Channel enforces specific actions)

**Why B:** Claude is smart; instructions guide, don't constrain.

---

### 8. Outbound messaging (Claude → DB)

- **[A] New tool from Channel** (`send_to_next_role`)
- **[B] Reuse existing tools** ✓ **CHOSEN**
  - Existing `send_message` tool works
  - Don't reinvent what's already tested
- **[C] Direct SQL** (Claude writes INSERT directly)

**Why B:** Reuse = less code, less risk.

---

### 9. Channel's MCP tools

- **[A] Minimal** (just notifications, no tools)
- **[B] Debug tools only** (`get_channel_status`)  ✓ **CHOSEN (POC)**
  - Single tool: `get_channel_status()` for debugging
  - No extra features
- **[C] Rich toolset** (status, query history, etc.)

**Why B:** Keep POC lean; add tools in Phase 2 if needed.

---

### 10. Crash resilience

- **[A] Persistent state file** (Channel remembers last rowid)
- **[B] Status filter** ✓ **CHOSEN**
  - Only watch `status='queued'`
  - If Channel crashes, message stays queued
  - Restart automatically re-detects
- **[C] Both** (belt and suspenders)

**Why B:** Status is our safety net; no extra state file needed.

---

### 11. Polling interval

- **[A] Fixed 5 seconds** ✓ **CHOSEN (POC)**
  - Simple, predictable, reasonable latency
- **[B] Per-role tunable** (specifier polls faster than architect)
- **[C] Adaptive** (back off if idle, accelerate if messages flowing)

**Why A:** Keep it simple; configurable later if needed.

---

### 12. Error handling strategy

- **[A] Fail silently** (log, continue polling)
- **[B] Fail fast on startup, log transient errors** ✓ **CHOSEN**
  - Config errors → exit immediately
  - Runtime errors → log warning, retry
  - Add debug logging to stderr
- **[C] Notify Claude** (push error notifications)

**Why B:** Fast failure catches bugs early; logging aids debugging.

---

### 13. MCP Architecture: Replace or Coexist?

- **[A] Replace mcp-sqlite** (Channel does all SQL)
- **[B] Parallel systems** (both in .mcp.json)
- **[C] Hybrid** ✓ **CHOSEN**
  - mcp-sqlite: explicit SQL queries (existing tools)
  - kiln-channel: notifications (new)
  - Separation of concerns
- **[D] Merge** (Channel wraps mcp-sqlite)

**Why C:** Clean separation; mcp-sqlite is proven, Channel is new.

---

### 14. Channel subprocess invocation

- **[A] Direct Python** ✓ **CHOSEN (PRIMARY)**
  - `python kiln/mcp-servers/db_channel_agent.py`
  - Simplest for POC, no npm needed
- **[B] Via npm/npx** (package as npm module)
  - Possible if we want package distribution
  - More overhead for POC
- **[C] Stdio wrapper** (Kiln spawns and manages stdio)
  - Standard MCP protocol
  - Clean but more complex startup logic

**Why A:** Python + FastMCP is simple; Kiln just spawns it.

**Fallback:** If subprocess lifecycle is hard, try **(B)** or **(C)**.

---

## Implementation Checklist

### Phase 1: POC (queued → delivered)

- [ ] Create `kiln/mcp-servers/db_channel_agent.py`
- [ ] Add environment variable injection to Kiln startup (kiln.ps1 / kiln.sh)
- [ ] Modify .mcp.json generation to include Channel entry
- [ ] Update CLAUDE.md template with Channel message-handling guidance
- [ ] Test 1: Manual DB insert → Channel detection → Claude notification
- [ ] Test 2: Full selftest chain with Channels instead of manual polling
- [ ] Document stderr logging output (debug reference)

### Phase 2: Refinement (future)

- [ ] Add `processed` status tracking
- [ ] Implement acknowledgment protocol
- [ ] Add richer debug tools
- [ ] Optimize polling (event-driven via SQLite triggers)
- [ ] Handle backpressure (what if Channel pushes faster than Claude consumes?)

---

## Known Unknowns & Risks

### Technical Unknowns (will discover in POC)

1. **Notification reliability** — Do all Claude notifications reach reliably, or can they be dropped if session is busy?
2. **Windows subprocess spawn** — Does Python subprocess lifetime work cleanly on Windows PowerShell?
3. **Database concurrency** — Will 4 Channels + mcp-sqlite hammer the DB, or does WAL handle it gracefully?
4. **Startup race conditions** — When Kiln spawns all agents + Channels simultaneously, will there be issues?

### Mitigations

- **Logging:** Debug output to stderr helps diagnose issues
- **Status filter:** If notification is dropped, message stays queued; Channel will re-detect
- **Testing:** Start with 1 agent + Channel, graduate to full swarm
- **Atomic updates:** Mark delivered before notification, so state is consistent

---

## References & Resources

- Kiln README: message schema, current workflow, MCP setup
- FastMCP docs: Python MCP server implementation
- SQLite3 docs: WAL mode, concurrent access, transactions
- Claude Code documentation: notification channels, MCP tools
