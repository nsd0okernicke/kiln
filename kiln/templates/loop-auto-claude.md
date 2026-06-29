# Message Loop

Repeat this sequence indefinitely. **Do not stop after completing work —
the loop is not complete until the handoff is sent (step 7).**

1. **Wait** — call `wait_for_message()` from the `kiln-channel` MCP server.
   If it returns `{"received": false}`, call it again immediately.
   Once a message arrives: immediately write its full content to `tmp/handoff-in.md`
   before doing anything else. If auto-compact fires and the tool result is lost,
   re-read `tmp/handoff-in.md` to restore it before proceeding to step 2.

2. **Merge** — extract the `Branch:` and `Commit:` fields from the handoff message, then run:
   ```sh
   git merge <commit-hash>
   ```
   This merge commit becomes the squash anchor you will need in step 6.

3. **Log received** — append a logbook.md entry: timestamp, full message content, one-line plan.

4. **Work** — apply your role rules. The Role section above defines your specific
   work process, quality gates, and standards. For system-communication-test messages,
   forward as-is to `{{HANDOFF_TARGET}}` and skip steps 5–7.

5. **Log sent** — append a logbook.md entry: timestamp, brief handoff summary. Commit as part of the squash in step 6.

6. **Squash** — squash all your commits since the merge commit into one:
   ```sh
   LAST_MERGE=$(git log --merges -1 --format="%H")
   git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
   git commit -m "{{COMMIT_FORMAT}}"
   ```

7. **Send handoff** — call `write_query` to INSERT into `messages` with `target='{{HANDOFF_TARGET}}'`,
   `branch='{{BRANCH}}'`, and `content` formatted per Handoff Message Format in Workflow Rules.
   Verify: `SELECT id FROM messages WHERE sender='{{ROLE}}' AND branch='{{BRANCH}}' AND status='queued' ORDER BY created_at DESC LIMIT 1`
   If no row is found, INSERT again before returning to step 1.

8. Return to step 1.
