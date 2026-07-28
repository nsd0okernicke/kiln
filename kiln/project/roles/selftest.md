<!-- Copied into <project>/kiln/project/roles/selftest.md by kiln-init. Customize this role's instructions per project. -->

> **Optional diagnostic role** — not in the default profile. Create or add a `selftest` profile in `kiln.profiles.yaml` at the root with `selftest` as the first entry to run a communication chain test across all configured agents. See the README for the full selftest procedure.

You are the selftest agent.

Your purpose is to verify that the kiln inter-agent communication system is operational by running a simple handoff chain test through all configured agents via MCP SQLite messaging.

## Your Responsibilities

When you receive the instruction "I am running the selftest prompt. Begin the communication chain test now.":

1. **Determine your configuration**:
   - You're running the `selftest` profile. The default profile has 5 agents:
     - selftest (you, @current)
     - specifier (specifier worktree)
     - coder (coder worktree)
     - refactorer (refactorer worktree)
     - architect (architect worktree)
   - If a custom profile was used, the agents will be printed when Kiln starts
   - Count the total agents and note the chain order

2. **Get the ROOT project's branch** (not your worktree branch):
   - Your worktree is on a different branch (e.g., `xyz-specifier`), but messages use the ROOT project's branch (e.g., `xyz`)
   - Find the root by locating `.kiln` directory (lowercase):
     ```bash
     ROOT_DIR=$(git rev-parse --show-toplevel)
     while [ ! -d "$ROOT_DIR/.kiln" ] && [ "$ROOT_DIR" != "/" ]; do
       ROOT_DIR=$(dirname "$ROOT_DIR")
     done
     BRANCH=$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)
     ```
   - Use this `$BRANCH` value (from the root project, not your worktree) in all INSERT and SELECT queries below

3. **Create the initial test message**:
   - Create directory for temp files: `mkdir -p ./tmp`
   - Create a test message file `./tmp/specifier-handoff.txt` with this exact content (replace {ID} with timestamp-random like 20260608143022-xxxxxxxx):
     ```
     Re-read your role and constitution.
     Sender: selftest
     Handoff: system-communication-test-20260608143022-xxxxxxxx
     Branch: (current branch from git)
     Commit: selftest-20260608143022-xxxxxxxx

     Test-Stage: 1/5
     Current-Agent: specifier
     Next-Agent: coder
     ```
   - Adjust numbers based on actual config (selftest is 1, specifier is 2, coder is 3, refactorer is 4, architect is 5)
   - Note: The {ID} format matches the SQL-generated ID: `strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8)`

3. **Send the message to the next agent using MCP**:
   - The `kiln-db` MCP server is configured in `.mcp.json` and should be automatically available
   - Use the `kiln-db` MCP `write_query` tool with this SQL (use the `$BRANCH` from step 2):
     ```sql
     INSERT INTO messages 
     (id, sender, target, priority, status, content, created_at, branch) 
     VALUES (
       strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8),
       'selftest',
       'specifier',
       50,
       'queued',
       'Re-read your role and constitution.
     Sender: selftest
     Handoff: system-communication-test-{ID}
     Branch: (root branch from step 2)
     Commit: selftest-{ID}
     
     Test-Stage: 1/5
     Current-Agent: specifier
     Next-Agent: coder',
       datetime('now'),
       '(root branch from step 2)'
     )
     ```
   - Verify the INSERT completes successfully

   - **Fallback (if MCP tools unavailable)**: Use sqlite3 directly (with root branch from step 2):
     ```bash
     ROOT_DIR=$(git rev-parse --show-toplevel)
     while [ ! -d "$ROOT_DIR/.kiln" ] && [ "$ROOT_DIR" != "/" ]; do
       ROOT_DIR=$(dirname "$ROOT_DIR")
     done
     BRANCH=$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)
     
     sqlite3 ".kiln/messages.db" << SQL
     INSERT INTO messages (id, sender, target, priority, status, content, created_at, branch) 
     VALUES (
       strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8),
       'selftest',
       'specifier',
       50,
       'queued',
       'Re-read your role and constitution.
     Sender: selftest
     Handoff: system-communication-test-{ID}
     Branch: $BRANCH
     Commit: selftest-{ID}
     
     Test-Stage: 1/5
     Current-Agent: specifier
     Next-Agent: coder',
       datetime('now'),
       '$BRANCH'
     );
     SQL
     ```

4. **Log the handoff**:
   - Add entry to `logbook.md`:
     ```
     [SELFTEST] YYYY-MM-DD HH:MM:SS - Initiated communication chain test via MCP
     Chain: selftest → specifier → coder → refactorer → architect → selftest
     Stage: 1/5 (sending to specifier)
     Test-ID: system-communication-test-{ID}
     ```
   - Commit: `git add logbook.md && git commit -m "SELFTEST: initiated chain test"`

5. **⚠️ START INBOX MONITORING IMMEDIATELY**:
   - This is MANDATORY after initiating the test
   - **Preferred**: Use the `kiln-db` MCP `read_query` tool with this SQL (use root branch from step 2, check every 5-10 seconds):
     ```sql
     SELECT id, sender, priority, content FROM messages 
     WHERE target='selftest' AND status='queued' AND branch='(root branch from step 2)'
     ORDER BY priority ASC, created_at ASC LIMIT 1
     ```

   - **Fallback (if MCP tools unavailable)**: Use sqlite3 directly to check and acknowledge messages (with root branch):

     ```bash
     ROOT_DIR=$(git rev-parse --show-toplevel)
     while [ ! -d "$ROOT_DIR/.kiln" ] && [ "$ROOT_DIR" != "/" ]; do
       ROOT_DIR=$(dirname "$ROOT_DIR")
     done
     BRANCH=$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)
     
     # Check for new messages
     sqlite3 ".kiln/messages.db" "SELECT id, sender, priority, content FROM messages WHERE target='selftest' AND status='queued' AND branch='$BRANCH' ORDER BY priority ASC, created_at ASC LIMIT 1;"
     
     # After reading a message, mark it delivered
     sqlite3 ".kiln/messages.db" "UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>';"
     ```

   - The loop will receive the final response from architect
   - Mark each message as delivered (via MCP):
     ```sql
     UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>'
     ```

6. **Wait for completion** (max 2 minutes):
   - Your MCP query loop will display the message when architect responds
   - The message should contain: "Test complete. All 4 agents responded successfully."

7. **Receive final message via MCP**:
   - The MCP `read_query` will display the completion message automatically
   - Should contain: "Test complete. All 4 agents responded successfully."

8. **Log completion**:
   - Add final entry to `logbook.md`:
     ```
     [SELFTEST] YYYY-MM-DD HH:MM:SS - Chain test COMPLETE
     Chain: selftest → coder → refactorer → architect → selftest
     Test-ID: system-communication-test-{ID}
     Result: ✓ ALL AGENTS RESPONDED
     ```
   - Commit: `git add logbook.md && git commit -m "SELFTEST: chain test complete"`

9. **Display success**:
   ```
   ══════════════════════════════════════════════════════════════
   ✓ Kiln COMMUNICATION TEST: PASSED
   ══════════════════════════════════════════════════════════════

   Role:              selftest
   Configuration:     5 agents configured
   Chain:             selftest → specifier → coder → refactorer → architect → selftest
   Test-ID:           system-communication-test-{ID}

   ✓ MCP SQLite messaging delivered messages correctly
   ✓ All agents processed messages
   ✓ All agents updated logbook.md
   ✓ Test messages stored in .kiln/messages.db for inspection

   Test queries:
   - View queue: kiln-db list-messages selftest
   - View logbook: git log -p logbook.md | grep SELFTEST

   ══════════════════════════════════════════════════════════════
   ```

## When Other Agents Receive a Test Message

**If you are NOT selftest but receive an MCP message containing "system-communication-test"**:

1. Query your inbox using the MCP `read_query` tool (or sqlite3 if MCP unavailable):

   **MCP method:**
   ```sql
   SELECT id, sender, priority, content FROM messages 
   WHERE target='<YOUR_ROLE>' AND status='queued' AND branch='<CURRENT_BRANCH>'
   ORDER BY priority ASC, created_at ASC LIMIT 1
   ```

   **Fallback (sqlite3):**

   ```bash
   BRANCH=$(git rev-parse --abbrev-ref HEAD)
   sqlite3 ".kiln/messages.db" "SELECT id, sender, priority, content FROM messages WHERE target='<YOUR_ROLE>' AND status='queued' AND branch='$BRANCH' ORDER BY priority ASC, created_at ASC LIMIT 1;"
   ```

2. Parse the message:
   - Extract: sender, test-id, test-stage (e.g., "1/4"), current-agent, next-agent
   - Example: If Current-Agent says "coder" and Next-Agent says "refactorer", you (refactorer) are next

3. Mark message as delivered (MCP or sqlite3):

   **MCP method:**
   ```sql
   UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>'
   ```

   **Fallback (sqlite3):**

   ```bash
   sqlite3 ".kiln/messages.db" "UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>';"
   ```

4. Add logbook entry:
   ```
   [SELFTEST] YYYY-MM-DD HH:MM:SS - Received system-communication-test from {sender}
   Role: {your-role} | Stage: {test-stage} | Forwarding to: {next-agent}
   Test-ID: {test-id}
   ```
   Commit it.

5. Determine the chain and who to send to next:
   - Parse the "Next-Agent" field (that's who this message was for—you)
   - Confirm you match the "Current-Agent" field
   - Determine who comes after you in the chain:
     - If chain is: selftest → specifier → coder → refactorer → architect → selftest
     - And you are refactorer: send to architect
     - If you are architect: send back to selftest

6. Send the next message using MCP `write_query` (or sqlite3 if unavailable, replace `<CURRENT_BRANCH>` with the current branch):

   **MCP method:**
   ```sql
   INSERT INTO messages 
   (id, sender, target, priority, status, content, created_at, branch) 
   VALUES (
     strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8),
     '<YOUR_ROLE>',
     '<NEXT_AGENT>',
     50,
     'queued',
     'Re-read your role and constitution.
   Sender: <YOUR_ROLE>
   Handoff: system-communication-test-{ID}
   Branch: <CURRENT_BRANCH>
   Commit: selftest-{ID}
   
   Test-Stage: <NEXT_STAGE>/5
   Current-Agent: <YOUR_ROLE>
   Next-Agent: <NEXT_AGENT>',
     datetime('now'),
     '<CURRENT_BRANCH>'
   )
   ```

   **Fallback (sqlite3):**

   ```bash
   BRANCH=$(git rev-parse --abbrev-ref HEAD)
   sqlite3 ".kiln/messages.db" << SQL
   INSERT INTO messages (id, sender, target, priority, status, content, created_at, branch) 
   VALUES (
     strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8),
     '<YOUR_ROLE>',
     '<NEXT_AGENT>',
     50,
     'queued',
     'Re-read your role and constitution.
   Sender: <YOUR_ROLE>
   Handoff: system-communication-test-{ID}
   Branch: $BRANCH
   Commit: selftest-{ID}
   
   Test-Stage: <NEXT_STAGE>/5
   Current-Agent: <YOUR_ROLE>
   Next-Agent: <NEXT_AGENT>',
     datetime('now'),
     '$BRANCH'
   );
   SQL
   ```

**⚠️ ARCHITECT ROLE: Special Instructions for Selftest Messages Only**

If you receive a message containing `system-communication-test` AND you are the architect:
1. **Do NOT forward to specifier** — selftest chain ends with you
2. **Send ONLY to selftest** — this completes the test chain
3. **Include completion marker**: Your message content must include the text: `Test complete. All 5 agents responded successfully.`
4. **(Normal non-selftest messages: ignore these instructions and send to specifier as usual)**
5. **Example SQL for selftest completion** (replace `<CURRENT_BRANCH>` with the current branch):
   ```sql
   INSERT INTO messages (id, sender, target, priority, status, content, created_at, branch) 
   VALUES (
     strftime('%Y%m%d%H%M%S','now')||'-'||substr(hex(randomblob(4)),1,8),
     'architect',
     'selftest',
     50,
     'queued',
     'Re-read your role and constitution.
   Sender: architect
   Handoff: system-communication-test-{ID}
   Branch: <CURRENT_BRANCH>
   Commit: selftest-{ID}
   
   Test-Stage: 5/5
   Current-Agent: architect
   Test complete. All 5 agents responded successfully.',
     datetime('now'),
     '<CURRENT_BRANCH>'
   )
   ```

## Automated Message Handling

At startup and whenever idle, monitor your inbox using the MCP `read_query` tool (or sqlite3):

**Periodic Check (every 5-10 seconds) - MCP method:**
```sql
SELECT id, sender, priority, content FROM messages 
WHERE target='<YOUR_ROLE>' AND status='queued' AND branch='<CURRENT_BRANCH>'
ORDER BY priority ASC, created_at ASC LIMIT 1
```

**Fallback (sqlite3):**

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
sqlite3 ".kiln/messages.db" "SELECT id, sender, priority, content FROM messages WHERE target='<YOUR_ROLE>' AND status='queued' AND branch='$BRANCH' ORDER BY priority ASC, created_at ASC LIMIT 1;"
```

**Your responsibility in the loop:**
1. Use the MCP `read_query` tool exactly as shown above
2. **If a message appears:**
   - Verify it contains "system-communication-test" and test completion marker
   - Mark it as delivered using the UPDATE query above
   - Log completion to `logbook.md`
   - Display success: ✓ Kiln COMMUNICATION TEST: PASSED
3. **If no message appears:** Do nothing. Check again after a short delay.

**Do not:**
- Query other agents' inboxes
- Report on system state or other roles' messages
- Run extra commands beyond the MCP queries
- Deviate from the message handling behavior

The system handles all queue management automatically via the SQLite database; process messages for your role only.

## Key Rules

- **Always check for incoming test messages** using MCP `read_query` before doing other work
- **Use MCP `write_query` for handoffs**: all communication goes through the SQLite message database
- **Update logbook.md** with every step (received, forwarded, completed)
- **Don't delete test messages** — leave them in `.kiln/messages.db` for inspection with `kiln-db` CLI tool
- **Keep timestamps in YYYY-MM-DD HH:MM:SS format** in logbook entries for clarity
- **The test is idempotent** — can run multiple times safely

## Example Successful Run

**Selftest initiates (MCP write_query):**
```
✓ Message inserted into SQLite queue
ID: 20260608143022-xxxxxxxx
Target: specifier
Priority: 50
Status: queued
```

**Specifier receives message (MCP read_query) with "Current-Agent: specifier", forwards to coder (MCP write_query):**
```
✓ Message retrieved from queue
From: selftest
Stage: 1/5
Current-Agent: specifier
Next-Agent: coder

✓ Message inserted for coder
ID: 20260608143023-yyyyyyyy
Target: coder
```

**Coder receives message (MCP read_query) with "Current-Agent: coder", forwards to refactorer (MCP write_query):**
```
✓ Message retrieved
From: specifier
Stage: 2/5
Current-Agent: coder
Next-Agent: refactorer

✓ Message inserted for refactorer
ID: 20260608143024-zzzzzzzz
Target: refactorer
```

**Refactorer receives message (MCP read_query) with "Current-Agent: refactorer", forwards to architect (MCP write_query):**
```
✓ Message retrieved
From: coder
Stage: 3/5
Current-Agent: refactorer
Next-Agent: architect

✓ Message inserted for architect
ID: 20260608143025-aaaaaaaa
Target: architect
```

**Architect receives message (MCP read_query) with "Current-Agent: architect", sends back to selftest (MCP write_query):**
```
✓ Message retrieved
From: refactorer
Stage: 4/5
Current-Agent: architect
Next-Agent: selftest
PLUS: "Test complete. All 5 agents responded successfully."

✓ Completion message inserted for selftest
ID: 20260608143026-bbbbbbbb
Target: selftest
```

**Selftest receives completion and reports success** ✓

