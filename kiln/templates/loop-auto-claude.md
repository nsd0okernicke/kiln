# Message Loop

**CRITICAL: "Work complete" or "tests pass" is NOT end-of-turn — neither is a verified
handoff, nor a returned subagent report. The wrapper must not stop early.
The turn is not over until Step 5 has also run: calling `/kiln-receive` again, in this same response.**

Repeat this sequence indefinitely:

1. **Receive** — run `/kiln-receive`. Handles: `wait_for_message()`, persist to
   `tmp/handoff-in.md`, auto-compact recovery, git merge, and log received.
   Do not proceed until the skill completes all its steps.

2. **Delegate the work** — do not implement anything yourself. Invoke the `Agent` tool
   with `subagent_type: "{{ROLE}}-worker"` and `run_in_background: false` (you must
   block here — later steps depend on its result). The prompt must be self-contained:
   include the full content of `tmp/handoff-in.md`, your current branch/worktree, and
   an explicit request for a final report of what was implemented/verified and which
   files were touched. The `{{ROLE}}-worker` subagent already has your role's work
   process, quality gates, and standards baked into its own definition — do not repeat
   them in the prompt, and do not do this work yourself.

   For `system-communication-test` messages: skip delegation entirely — forward the
   message as-is to `{{HANDOFF_TARGET}}` using `/kiln-handoff` immediately.

3. **Handle a failed or blocked report** — if the subagent's report says it could not
   finish (blocked, failing tests, unclear task), invoke it again once more, in this
   same turn, including its failure report as feedback in the new prompt. If it fails a
   second time, do not retry further: proceed to Step 4 with a handoff that reports the
   blocker instead of normal work, so the loop keeps moving instead of stalling silently.

4. **Send handoff** — run `/kiln-handoff`. Handles: log sent, squash, INSERT into
   messages, verify, and retry. Do not return to Step 1 until the skill confirms
   a queued row in the database.

5. **Immediately return to Step 1** — call `/kiln-receive` now, in this same turn.
