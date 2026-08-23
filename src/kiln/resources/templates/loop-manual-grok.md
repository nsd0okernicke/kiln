# Interaction Loop

**On first startup**: call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first, then greet the user and ask what to work on. Begin at Step 2 on
first startup only — do not poll yet.

**CRITICAL: "Work complete" or "approval received" is NOT end-of-turn — and neither is a
verified handoff. The turn is not over until Step 5 has also run, returning to Step 1 in
this same response. Do not stop, summarize, or wait for the user to say anything
between Step 4 and Step 5.**

Repeat this sequence indefinitely on subsequent cycles. You receive messages by polling
`kiln-db` directly — Grok has no confirmed support for a long-blocking MCP tool call, so
unlike Claude's `/kiln-receive` (which blocks on `kiln-channel`'s `wait_for_message()`), this
loop polls the same way Codex's and Copilot's do. Do not call `wait_for_message()` — that's a
`kiln-channel` tool and this role's `.mcp.json` only has `kiln-db`.

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition (you may see the command fail silently if the status dir doesn't exist yet — that's harmless).

1. **Receive** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first. Then call `query`:
   ```sql
   SELECT id, sender, content, created_at
   FROM messages
   WHERE target='{{ROLE}}' AND branch='{{BRANCH}}' AND status='queued'
   ORDER BY priority ASC, created_at ASC
   LIMIT 1
   ```
   If the result is empty, wait 15 seconds and repeat step 1.
   When a row is found, immediately call `python .kiln/tools/set-status.py {{ROLE}} receiving --mode={{MODE}}`, then mark it delivered:
   ```sql
   UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<id>'
   ```
   Immediately write the full message content verbatim to `tmp/handoff-in.md`, merge the
   sender's commit (`git merge <commit-hash>` using the `Commit:` field from the message —
   this becomes the squash anchor `/kiln-handoff` uses in Step 4), and log a `[RECEIVED]`
   entry to `logbook.md` (timestamp, full message content) — if context is compacted later
   in the cycle and this tool result is gone, re-read `tmp/handoff-in.md` to restore it
   rather than re-polling.

2. **Work** — call `python .kiln/tools/set-status.py {{ROLE}} working --mode={{MODE}}` first. Then apply your role rules. The Role section above defines your work process.

3. **Get approval** — call `python .kiln/tools/set-status.py {{ROLE}} approval --mode={{MODE}}` first. Then present your result to the user and ask for explicit approval.
   Do not continue to Step 4 without approval.

4. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry (all via `kiln-db` — no `kiln-channel` dependency). Do not return
   to Step 1 until the skill confirms a queued row in the database.

5. **Immediately return to Step 1**, in this same turn, without waiting for the user to say
   anything further. (Step 1 will re-emit the `waiting` status at its start.)
