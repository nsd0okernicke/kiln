# Message Loop

**CRITICAL: "Work complete" or "tests pass" is NOT end-of-turn — neither is a verified
handoff, nor a returned subagent report. The wrapper must not stop early.
The turn is not over until Step 7 has also run, returning to Step 1 in this same response.**

Repeat this sequence indefinitely. You receive messages by polling `kiln-db` directly — Codex
has no confirmed support for a long-blocking MCP tool call, so unlike Claude's `/kiln-receive`
(which blocks on `kiln-channel`'s `wait_for_message()`), this loop polls the same way Copilot's
does. Do not call `wait_for_message()`, `mark_processing()`, or `mark_processed()` — those are
`kiln-channel` tools and this role's MCP config only has `kiln-db`.

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition (you may see the command fail silently if the status dir doesn't exist yet — that's harmless).

1. **Poll** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first. Then call `query`:
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
   Immediately write the full message content verbatim to `tmp/handoff-in.md` — if auto-compact
   fires later in the cycle and this tool result is gone from context, re-read that file to
   restore it rather than re-polling.

2. **Merge** — extract `Branch:` and `Commit:` from the message content, then run:
   ```sh
   git merge <commit-hash>
   ```
   This merge commit becomes the squash anchor `/kiln-handoff` uses in step 6.

3. **Log received** — append a logbook.md entry: timestamp, full message content.

4. **Delegate the work** — call `python .kiln/tools/set-status.py {{ROLE}} delegating {{ROLE}}-worker --mode={{MODE}}` first. Then do not implement anything yourself. Delegate this task entirely to the
   custom agent named `{{ROLE}}-worker` (defined in `.codex/agents/{{ROLE}}-worker.toml`) using your multi-agent spawn tools. The prompt must be self-contained: include the full content of `tmp/handoff-in.md`, your current branch/worktree, and an explicit request for a final report of what was implemented/verified and which files were touched. The `{{ROLE}}-worker` agent already has your role's work process, quality gates, and standards baked into its own definition — do not repeat them in the task, and do not do this work yourself.

   For `Kiln-Ping: true` messages: skip delegation and step 5 entirely — this is a health-check
   ping, not real work. Extract the `Trail:` list from the message and append one line for
   yourself: `- {{ROLE}} ({{BRANCH}})`. Determine your target the same way you would for a normal
   handoff — your entry in the Handoff Routing table (Workflow Rules), including any
   role-specific override your own role file instructs (never hardcode a static target). Continue
   to step 6 to send the updated ping onward (`/kiln-handoff` handles the log/squash internally).

5. **Handle a failed or blocked report** — if the worker's report says it could not
   finish (blocked, failing tests, unclear task), delegate to it again once more, in this
   same turn, including its failure report as feedback. If it fails a second time, do not retry further: proceed to Step 6 with a handoff that reports the blocker instead of normal work, so the loop keeps moving instead of stalling silently.

6. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry (all via `kiln-db` — no `kiln-channel` dependency). Do not return
   to Step 1 until the skill confirms a queued row in the database.

7. **Immediately return to Step 1**, in this same turn.
