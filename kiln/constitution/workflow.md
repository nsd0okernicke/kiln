# Workflow Rules

## Message Queue

Kiln uses a SQLite message database at `.Kiln/messages.db` in the project root for all inter-agent communication. Each agent has direct access via the **`Kiln-db` MCP server** configured in `.claude/settings.json` and `.mcp.json`.

### Check Your Inbox

At session start and after completing any task, check for waiting messages using the `read_query` MCP tool. The exact SQL is in your CLAUDE.md Runtime section. When a message is returned, process it according to your role instructions. Then mark it delivered immediately using `write_query`:

```sql
UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<message-id>'
```

### Send a Message (Handoff)

Use `write_query` with an INSERT. The exact SQL template is in your CLAUDE.md Runtime section. Include the complete handoff message in the `content` column.

**Priority values:**
- `0-9`: High priority (architect handoffs, critical tasks)
- `50`: Normal priority (standard handoffs and messages)
- `100+`: Low priority (informational messages)

- The project root is the directory containing `.Kiln/`. From a named worktree (e.g., `.worktrees/coder/`), walk up parent directories until you find it.
- At startup, discover and remember the branch or worktree assigned to your role.
- If your assigned worktree is `@current`, `master`, or `none`, work in the main project checkout on its current branch; do not expect or create a `.worktrees/<role>` directory for that role.
- When one role has `@current` and another has a named worktree (e.g., `coordinator` with `@current` and `coder` with `coder`): the `@current` role works in the main directory on the current branch, while the named-worktree role works in `.worktrees/coder` on a sub-branch named `<current-branch>-coder`. Both roles see the same HEAD branch, but from different worktrees and branches.
- Work only in your assigned branch or worktree.
- Do not inspect, diff, merge, or base work on another branch unless that branch is specifically named in a handoff or explicit user instruction.
- Use `./tmp/` in your assigned worktree for temporary files; do not use `/tmp`.
- For handoffs, use the MCP `Kiln-db` `write_query` tool to send messages directly to the database.
- Maintain the tracked root `logbook.md` file for every handoff received or sent.
- When receiving a handoff, add a `logbook.md` note with a timestamp, the complete handoff message received, and a brief description of the action taken.
- When sending a handoff, add a `logbook.md` note with a timestamp, the complete handoff message sent, and a brief summary of the handoff contents.
- Commit `logbook.md` updates with the related work; do not leave handoff log entries untracked or uncommitted.
- Start every handoff message with: `Re-read your role and constitution.`
- The specifier invents a short, stable handoff name for each accepted specification handoff.
- Every later handoff for that work must include the specifier handoff name.
- Handoffs must report only essential state, not prescribe process. After the opening line, include exactly these fields and no other prose: sender role, specifier handoff name, branch name, and commit hash.
- Do not tell the receiving role how to do its job, repeat your process, or ask it to continue sender-owned responsibilities. The normal request is: `Apply your own role rules to this state.`
- After receiving a handoff, merge the sender branch state identified by the handoff branch name and commit hash into your assigned branch before applying your own role rules.
- When receiving a handoff, ignore sender process narrative and decide next actions only from your own role prompt, the constitution, and the current project state.
- If the expected git layout or assigned worktree is missing, stop and report instead of silently working in the wrong place.

