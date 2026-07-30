# Message Loop

**CRITICAL: "Work complete" or "tests pass" is NOT end-of-turn — neither is a verified
handoff, nor a returned subagent report. The wrapper must not stop early.
The turn is not over until Step 7 has also run: calling `/kiln-receive` again, in this same response.**

Repeat this sequence indefinitely:

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition (you may see the command fail silently if the status dir doesn't exist yet — that's harmless).

1. **Receive** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first. Then run `/kiln-receive`. Handles: `wait_for_message()`, persist to
   `tmp/handoff-in.md`, auto-compact recovery, git merge, and log received.
   Do not proceed until the skill completes all its steps.
   Extract the `id` field from the `/kiln-receive` result and save it for Step 2.

2. **Mark as processing** — Immediately call the `mark_processing(message_id)` MCP tool to transition the message from `delivered` to `processing` state. This signals that work has begun.

3. **Delegate the work** — call `python .kiln/tools/set-status.py {{ROLE}} delegating {{ROLE}}-worker --mode={{MODE}}` first. Then do not implement anything yourself. Delegate this task entirely to the
   custom agent named `{{ROLE}}-worker` (defined in `.codex/agents/{{ROLE}}-worker.toml`) using your multi-agent spawn tools. The prompt must be self-contained: include the full content of `tmp/handoff-in.md`, your current branch/worktree, and an explicit request for a final report of what was implemented/verified and which files were touched. The `{{ROLE}}-worker` agent already has your role's work process, quality gates, and standards baked into its own definition — do not repeat them in the task, and do not do this work yourself.

4. **Handle a failed or blocked report** — if the worker's report says it could not
   finish (blocked, failing tests, unclear task), delegate to it again once more, in this
   same turn, including its failure report as feedback. If it fails a second time, do not retry further: proceed to Step 5 with a handoff that reports the blocker instead of normal work, so the loop keeps moving instead of stalling silently.

5. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry. Do not return to Step 1 until the skill confirms
   a queued row in the database.

6. **Mark as processed** — Call `mark_processed(message_id)` to transition the message from `processing` to `processed` state, signaling completion. This completes the handoff cycle.

7. **Immediately return to Step 1** — call `/kiln-receive` now, in this same turn. (Step 1 will re-emit the `waiting` status at its start.)
