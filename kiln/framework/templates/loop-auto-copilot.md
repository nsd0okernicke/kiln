# Message Loop

Repeat this sequence indefinitely. You receive messages by polling — there is no
blocking channel. **Do not stop after completing work — the loop is not complete
until the handoff is sent (step 8).**

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition.

1. **Poll** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first. Then call `read_query`:
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

2. **Merge** — extract `Branch:` and `Commit:` from the message content, then run:
   ```sh
   git merge <commit-hash>
   ```
   This merge commit becomes the squash anchor for step 7.

3. **Log received** — append a logbook.md entry: timestamp, full message content.

4. **Delegate the work** — call `python .kiln/tools/set-status.py {{ROLE}} delegating {{ROLE}}-worker --mode={{MODE}}` first. Then do not implement anything yourself. Delegate this task entirely to the
   custom agent named `{{ROLE}}-worker` (defined in `.github/agents/{{ROLE}}-worker.agent.md`). Give
   it the full content of the received message, your current branch/worktree, and an explicit request
   for a final report of what was implemented/verified and which files were touched. The
   `{{ROLE}}-worker` agent already has your role's work process, quality gates, and standards baked
   into its own definition — do not repeat them in the prompt, and do not do this work yourself.

   For system-communication-test messages: skip delegation entirely — forward the message as-is to
   `{{HANDOFF_TARGET}}` and skip steps 5–8.

5. **Handle a failed or blocked report** — if the worker's report says it could not finish (blocked,
   failing tests, unclear task), delegate to it again once more, in this same turn, including its
   failure report as feedback. If it fails a second time, do not retry further: proceed to step 6 with
   a handoff that reports the blocker instead of normal work, so the loop keeps moving instead of
   stalling silently.

6. **Log sent** — append a logbook.md entry: timestamp, brief summary. Commit as part of the squash in step 7.

7. **Squash** — squash all your commits since the merge commit:
   ```sh
   LAST_MERGE=$(git log --merges -1 --format="%H")
   git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
   git commit -m "{{COMMIT_FORMAT}}"
   ```

8. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then call `write_query` to INSERT into `messages` with `target='{{HANDOFF_TARGET}}'`,
   `branch='{{BRANCH}}'`, and `content` formatted per Handoff Message Format in Workflow Rules.
   Verify: `SELECT id FROM messages WHERE sender='{{ROLE}}' AND branch='{{BRANCH}}' ORDER BY created_at DESC LIMIT 1`
   If no row is found, INSERT again before returning to step 1.

9. Return to step 1.
