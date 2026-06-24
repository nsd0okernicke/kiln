# Message Loop

Repeat this sequence indefinitely. You receive messages by polling — there is no
blocking channel. **Do not stop after completing work — the loop is not complete
until the handoff is sent (step 7).**

1. **Poll** — call `read_query`:
   ```sql
   SELECT id, sender, content, created_at
   FROM messages
   WHERE target='{{ROLE}}' AND branch='{{BRANCH}}' AND status='queued'
   ORDER BY priority ASC, created_at ASC
   LIMIT 1
   ```
   If the result is empty, wait 15 seconds and repeat step 1.
   When a row is found, immediately mark it delivered:
   ```sql
   UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<id>'
   ```

2. **Merge** — extract `Branch:` and `Commit:` from the message content, then run:
   ```sh
   git merge <commit-hash>
   ```
   This merge commit becomes the squash anchor for step 6.

3. **Log received** — append a logbook.md entry with timestamp and full message content.

4. **Work** — apply your role rules. The Role section above defines your specific work process.
   For system-communication-test messages, forward as-is to `{{HANDOFF_TARGET}}` and skip
   steps 5–7.

5. **Log sent** — append a logbook.md entry with timestamp and brief summary.
   Commit it as part of the squash in step 6.

6. **Squash** — squash all your commits since the merge commit:
   ```sh
   LAST_MERGE=$(git log --merges -1 --format="%H")
   git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
   git commit -m "{{COMMIT_FORMAT}}"
   ```

7. **Send handoff** — call `write_query` to INSERT into `messages` with `target='{{HANDOFF_TARGET}}'`,
   `branch='{{BRANCH}}'`, and `content` formatted per Handoff Message Format in Workflow Rules.
   Verify: `SELECT id FROM messages WHERE sender='{{ROLE}}' AND branch='{{BRANCH}}' ORDER BY created_at DESC LIMIT 1`
   If no row is found, INSERT again before returning to step 1.

8. Return to step 1.
