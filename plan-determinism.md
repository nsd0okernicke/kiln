# Plan: Determinism & Multi-Agent Compatibility

## Context

Kiln runs 4 persistent Claude agents communicating via SQLite messages. The explicit numbered Message Loop (wait → merge → work → squash → handoff → repeat) was implemented to fix most reliability issues. The current failure rate is ~10%: agents complete work but forget to send the handoff, or stop after tests pass. This plan addresses both further hardening determinism and making the messaging system work for non-Claude agents (Copilot, Codex, Grok).

---

## Current Failure Mode Analysis

### What fails (~10% of cycles)
- Agent completes the task but does not execute step 7 (handoff INSERT)
- Agent treats "tests pass" as end condition despite the explicit loop header warning
- Context pressure over a long session buries the loop instructions

### What's already done
- Numbered Message Loop in every role file (merge is step 2, handoff is step 7)
- `wait_for_message()` is a proper blocking MCP tool — not manual SQL polling
- Logbook commit order fixed (log-sent before squash, both inside the commit)
- Explicit IMPORTANT branch warning in generated CLAUDE.md SQL template
- Mandatory language at loop header ("do not stop after tests pass")

### What's still probabilistic
- No enforcement that the loop actually completes before the turn ends
- No verification that the handoff INSERT succeeded
- No retry mechanism when an agent silently drops the last step
- `wait_for_message()` is Claude-specific; non-Claude agents have no usable inbox mechanism

---

## Track A — Prompt Hardening (Low Effort / Medium Gain)

### A1: Self-verification after handoff INSERT

Add a confirmation sub-step to the handoff step in each role's Message Loop. After calling `write_query` to INSERT:

```text
7. Send handoff — INSERT via write_query. Then immediately call read_query:
   SELECT id, status FROM messages WHERE sender='<your-role>' AND branch='<branch>'
   ORDER BY created_at DESC LIMIT 1
   Verify the row exists and status='queued'. If not, INSERT again.
```

This adds one cheap SQL read that forces the agent to confirm success before returning to step 1.

### A2: Self-state summary at loop top

At step 1 (and after the merge), have each agent emit a one-line status to the logbook acknowledging which cycle it is on. This keeps the loop salient in context:

```text
> Cycle N: received from <sender>, handoff-name=<name>, commit=<hash>
```

Not a logbook.md write — just the internal reasoning summary the agent already outputs. Keeping cycle-tracking visible makes "I just started cycle N, I must complete all 8 steps" more reliable.

---

## Track B — Claude Code Hooks (Medium Effort / High Gain)

Hooks are the highest-leverage improvement for always-on agents. They run **deterministic shell code** at fixed lifecycle points, independently of what the LLM decides, and never get buried by context pressure.

### B1: Stop hook — enforce handoff before turn ends

The `Stop` hook fires at the end of every agent turn. A small script checks whether the agent sent a handoff since the last `wait_for_message()` call.

**Script** `kiln/hooks/enforce-handoff.ps1`:
```powershell
# Called by Claude Code at end of every turn
# Env vars available: KILN_ROLE, KILN_DB_PATH, KILN_BRANCH
# Reads Claude hook context from stdin (JSON)
$ctx = $input | ConvertFrom-Json

# Read recent sent messages from this agent
$recentHandoff = python -c @"
import sqlite3, sys, os, json
db = os.environ['KILN_DB_PATH']
role = os.environ['KILN_ROLE']
branch = os.environ['KILN_BRANCH']
conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT id FROM messages WHERE sender=? AND branch=? AND created_at > datetime('now','-120 seconds') LIMIT 1",
    (role, branch)
).fetchone()
conn.close()
print('found' if row else 'missing')
"@

if ($recentHandoff -eq 'missing') {
    # Block the turn and inject feedback
    $feedback = @{
        decision = "block"
        reason = "No handoff message was sent in this turn. You must complete step 7: INSERT a handoff via write_query before ending. Call write_query now."
    } | ConvertTo-Json
    Write-Output $feedback
}
```

The hook returns `{ "decision": "block", "reason": "..." }` to force the agent to keep working until it sends the handoff. Claude Code feeds the reason back into the session as a corrective prompt.

**Caveat on timing**: The hook should only block if the agent appears to have done work (i.e., git activity or file writes happened this turn). Otherwise it blocks on idle wait cycles too. The script can check this via a brief `git diff --stat HEAD` or by examining what tools were called (available in the hook context JSON from stdin).

### B2: PostToolUse hook — verify write_query success

After every `write_query` tool call, a lightweight hook logs the affected row count and can flag zero-row inserts:

```powershell
# PostToolUse hook — tool_name=write_query
$ctx = $input | ConvertFrom-Json
if ($ctx.tool_name -eq 'write_query' -and $ctx.tool_result -notmatch '"rows_affected":\s*[1-9]') {
    @{ decision = "block"; reason = "write_query reported 0 rows affected. The INSERT may have failed. Check the SQL and retry." } | ConvertTo-Json
}
```

### B3: Wire hooks into generated `.claude/settings.json`

The `settings.json` template (`kiln/.claude/settings.json`) gets copied to every worktree by `Write-ClaudeConfig`. The Stop and PostToolUse hooks need to be added to this template. The hook commands must use absolute paths (available via `KILN_DIR` env var injected by kiln.ps1).

The hook paths need to be resolvable from within the worktree. The simplest approach: write absolute hook paths into the generated settings.json at startup time (same pattern as how `.mcp.json` gets generated with absolute DB paths).

---

## Track C — Watcher Process (High Effort / Near-100% Reliability)

A thin deterministic supervisor process (`kiln/mcp-server/watcher.py`) that monitors the workflow and injects corrective messages when agents go silent after expected work.

### Architecture

```
DB: messages table (existing)
DB: workflow_state table (new)
    - agent TEXT, branch TEXT, state TEXT, last_updated TEXT
    - states: WAITING | EXECUTING | COMMITTED | HANDOFF_SENT
```

The watcher:
1. Polls `workflow_state` every 10 seconds
2. Detects agents stuck in `EXECUTING` longer than a configurable timeout (default: 15 min)
3. For stuck agents: INSERTs a corrective message into the agent's own inbox:
   ```
   SYSTEM: You appear to have completed work but the handoff was not sent.
   Your current state is EXECUTING. Complete step 7: send the handoff now via write_query.
   ```
4. Agents update their own state by calling a new `update_state` tool exposed by the watcher MCP server (or by the watcher reading git activity as a proxy for state transitions)

### State transitions (driven by agents calling existing tools)
- `wait_for_message()` returns a message → watcher sees delivered message → sets `EXECUTING`
- `write_query` INSERT to messages → watcher detects new outgoing message → sets `HANDOFF_SENT`
- `wait_for_message()` called again → sets `WAITING`

The watcher can infer transitions by watching DB events without requiring agents to call any new tools.

### Integration with kiln.ps1
- New optional `-Watcher` switch on `kiln.ps1`
- Adds `workflow_state` table to DB init script
- Launches `watcher.py` as an additional background process (no terminal pane needed)
- `-Stop` flag already kills python.exe processes matching `channel.py`; extend pattern to also match `watcher.py`

---

## Track D — Non-Claude Agent Compatibility (Low Effort / Unblocks Future Agents)

### Problem

`wait_for_message()` is a Python asyncio blocking tool. Claude Code can hold an MCP tool call open indefinitely. Copilot, Codex, and similar agents:
- May not support long-blocking MCP calls
- Currently receive only `kiln-db` in their MCP config (`Prepare-AgentConfigs` writes `~/.copilot/mcp-config.json` without `kiln-channel`)
- Have no way to receive push notifications; they can only poll

### D1: Add `poll_for_message()` to `channel.py`

A new non-blocking tool — single DB check, returns immediately:

```python
@mcp.tool()
def poll_for_message() -> dict:
    """
    Check once for a queued message addressed to this role. Returns immediately.
    Returns {"received": true, ...} if a message is waiting, or {"received": false} if not.
    Call this in a retry loop with a delay between calls.
    """
    msg = _fetch_and_deliver()
    if msg:
        return {"received": True, **msg}
    return {"received": False}
```

This is complementary to `wait_for_message()` — the same `_fetch_and_deliver()` function, just without the polling loop.

### D2: Agent-type-aware messaging instructions in generated config files

`Write-GeneratedCLAUDEmd` in `kiln.ps1` already branches on `$Agent` (Claude gets `CLAUDE.md`, Copilot gets `.github/copilot-instructions.md`). Extend it to emit different "Receiving Messages" instructions:

**For Claude** (current behavior — no change):
```markdown
### Receiving Messages
Call wait_for_message() from kiln-channel. It blocks until a message arrives.
```

**For Copilot / other agents (pull mode)**:
```markdown
### Receiving Messages (Pull Mode)
You do not have a blocking channel. Use this polling loop:

1. Call read_query:
   SELECT id, sender, content, created_at FROM messages
   WHERE target='<role>' AND branch='<branch>' AND status='queued'
   ORDER BY priority ASC, created_at ASC LIMIT 1
2. If result is empty: wait 15 seconds, then repeat step 1.
3. When a message is found, immediately mark it delivered:
   UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>'
4. Proceed with step 2 of the Message Loop (Merge).

Do not proceed to the Merge step until you have marked the message delivered.
```

If `kiln-channel` is available to the non-Claude agent (see D3), it can use `poll_for_message()` instead of raw SQL.

### D3: Expose `kiln-channel` (poll mode) to non-Claude agents

Update `Prepare-AgentConfigs` in `kiln.ps1` to include `kiln-channel` in Copilot's `mcp-config.json`, configured per-role:

```json
{
  "mcpServers": {
    "kiln-db": { "command": "npx", "args": ["mcp-sqlite", "<db>"] },
    "kiln-channel": {
      "command": "python",
      "args": ["<channel.py>"],
      "env": {
        "KILN_ROLE": "<role>",
        "KILN_DB_PATH": "<db>",
        "KILN_BRANCH": "<branch>"
      }
    }
  }
}
```

Copilot would then call `poll_for_message()` in a retry loop (role-file instructions handle the loop). The channel server handles the atomic mark-delivered, so there's no race between multiple agents checking the same inbox.

**Limitation**: This requires Copilot to support one MCP config per role (not one global config). The current `Prepare-AgentConfigs` writes a single `~/.copilot/mcp-config.json` — this needs to move to per-worktree `.copilot/mcp-config.json` if Copilot supports it, or a role-scoped alternative.

### D4: Role files — add pull-mode note

Each role's Message Loop step 1 currently reads:
```
1. Wait — call wait_for_message() from the kiln-channel MCP server.
```

Update to:
```
1. Wait — call wait_for_message() from kiln-channel (Claude agents) or
   poll_for_message() in a retry loop (non-Claude agents). See your
   CLAUDE.md / copilot-instructions.md Receiving Messages section for
   the exact instructions for your agent type.
```

This keeps the role file agent-agnostic while the generated config file provides the agent-specific implementation.

---

## Implementation Order

| Phase | Track | Change | Effort | Gain |
|-------|-------|--------|--------|------|
| 1 | A1 | Add self-verification after handoff INSERT in all 4 role files | Low | Medium |
| 2 | B1 | `enforce-handoff.ps1` Stop hook + wire into settings.json generation | Medium | High |
| 2 | B2 | `verify-write.ps1` PostToolUse hook | Low | Medium |
| 3 | D1 | `poll_for_message()` in `channel.py` | Low | Unblocks non-Claude |
| 3 | D2 | Agent-type-aware instructions in `Write-GeneratedCLAUDEmd` | Low | Unblocks non-Claude |
| 3 | D3 | Per-role `kiln-channel` config for Copilot in `Prepare-AgentConfigs` | Medium | Unblocks non-Claude |
| 3 | D4 | Pull-mode note in role files (step 1 of Message Loop) | Low | Clarity |
| 4 | C  | Watcher process + workflow_state table | Medium-High | Near-100% |

Phases 1–3 are independent of each other and can be done in any order. Phase 4 depends on the DB schema additions but not on Phases 1–3.

---

## What This Does NOT Change

- The Message Loop numbered sequence in each role file stays intact — it is the correct behavioral spec
- `wait_for_message()` blocking behavior stays — it is correct for Claude agents
- The SQLite-based messaging DB stays as the single source of truth
- The always-on multi-pane terminal UX stays unchanged
- kiln.ps1 orchestration stays PowerShell — the watcher is an optional addition, not a replacement
