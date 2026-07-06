# Message Loop

**CRITICAL: "Work complete" or "tests pass" is NOT end-of-turn. The cycle ends only
when the handoff is sent and verified (Step 3).**

Repeat this sequence indefinitely:

1. **Receive** — run `/kiln-receive`. Handles: `wait_for_message()`, persist to
   `tmp/handoff-in.md`, auto-compact recovery, git merge, and log received.
   Do not proceed until the skill completes all its steps.

2. **Work** — apply your role rules. The Role section above defines your work process,
   quality gates, and standards.
   For `system-communication-test` messages: forward as-is to `{{HANDOFF_TARGET}}`
   using `/kiln-handoff` immediately — skip normal work.

3. **Send handoff** — run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry. Do not return to Step 1 until the skill confirms
   a queued row in the database.

4. Return to Step 1.
