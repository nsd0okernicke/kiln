# Interaction Loop

**On first startup**: call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first, then greet the user and ask what to work on. Begin at Step 2 on
first startup only — do not call `wait_for_message()`.

**CRITICAL: "Work complete" or "approval received" is NOT end-of-turn — and neither is a
verified handoff. The turn is not over until Step 5 has also run: calling `/kiln-receive`
again, in this same response. Do not stop, summarize, or wait for the user to say anything
between Step 4 and Step 5.**

Repeat this sequence indefinitely on subsequent cycles:

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition (you may see the command fail silently if the status dir doesn't exist yet — that's harmless).

1. **Receive** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first (while blocking on `wait_for_message()`). Then run `/kiln-receive`. Handles: `wait_for_message()`, persist to
   `tmp/handoff-in.md`, auto-compact recovery, git merge, and log received.
   Do not proceed until the skill completes all its steps.
   Note: `/kiln-receive` will upgrade status to "receiving" once a message arrives.

2. **Work** — call `python .kiln/tools/set-status.py {{ROLE}} working --mode={{MODE}}` first. Then apply your role rules. The Role section above defines your work process.
   For `system-communication-test` messages: forward as-is to `{{HANDOFF_TARGET}}`
   using `/kiln-handoff` immediately — skip normal work and approval.

3. **Get approval** — call `python .kiln/tools/set-status.py {{ROLE}} approval --mode={{MODE}}` first. Then present your result to the user and ask for explicit approval.
   Do not continue to Step 4 without approval.

4. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry. Do not return to Step 1 until the skill confirms
   a queued row in the database.

5. **Immediately return to Step 1** — call `/kiln-receive` now, in this same turn, without
   waiting for the user to say anything further. (Step 1 will re-emit the `waiting` status at its start.)
