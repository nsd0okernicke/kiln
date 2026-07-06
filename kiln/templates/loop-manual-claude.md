# Interaction Loop

**On first startup**: greet the user and ask what to work on. Begin at Step 2 on
first startup only — do not call `wait_for_message()`.

**CRITICAL: "Work complete" or "approval received" is NOT end-of-turn. The cycle ends
only when the handoff is sent and verified (Step 4).**

Repeat this sequence indefinitely on subsequent cycles:

1. **Receive** — run `/kiln-receive`. Handles: `wait_for_message()`, persist to
   `tmp/handoff-in.md`, auto-compact recovery, git merge, and log received.
   Do not proceed until the skill completes all its steps.

2. **Work** — apply your role rules. The Role section above defines your work process.
   For `system-communication-test` messages: forward as-is to `{{HANDOFF_TARGET}}`
   using `/kiln-handoff` immediately — skip normal work and approval.

3. **Get approval** — present your result to the user and ask for explicit approval.
   Do not continue to Step 4 without approval.

4. **Send handoff** — run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry. Do not return to Step 1 until the skill confirms
   a queued row in the database.

5. Return to Step 1.
