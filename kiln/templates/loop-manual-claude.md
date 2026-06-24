# Interaction Loop

**On first startup**: greet the user and ask what to work on. Do not call
`wait_for_message()` at startup. Begin at step 4 on first startup only.

Repeat this sequence indefinitely on subsequent cycles:

1. **Wait** — call `wait_for_message()` from the `kiln-channel` MCP server.
   If it returns `{"received": false}`, call it again immediately.

2. **Merge** — extract the `Branch:` and `Commit:` fields from the handoff message, then run:
   ```sh
   git merge <commit-hash>
   ```
   This merge commit becomes the squash anchor you will need in step 7.

3. **Log received** — append a logbook.md entry with timestamp and full handoff message.

4. **Work** — apply your role rules. The Role section above defines your specific work process.
   For system-communication-test messages, forward as-is to `{{HANDOFF_TARGET}}` and skip
   steps 5–8.

5. **Get approval** — present your result to the user and ask for explicit approval.
   Do not continue to step 6 without approval.

6. **Log sent** — append a logbook.md entry with timestamp and brief summary.
   This entry must be committed as part of the squash in step 7.

7. **Squash** — squash all your commits since the merge commit into one:
   ```sh
   LAST_MERGE=$(git log --merges -1 --format="%H")
   git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
   git commit -m "{{COMMIT_FORMAT}}"
   ```

8. **Send handoff** — call `write_query` to INSERT into `messages` with `target='{{HANDOFF_TARGET}}'`,
   `branch='{{BRANCH}}'`, and `content` formatted per Handoff Message Format in Workflow Rules.
   Verify: `SELECT id FROM messages WHERE sender='{{ROLE}}' AND branch='{{BRANCH}}' ORDER BY created_at DESC LIMIT 1`
   If no row is found, INSERT again before returning to step 1.

9. Return to step 1.
