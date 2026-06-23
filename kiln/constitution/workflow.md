# Workflow Rules

## Message Queue

Kiln uses a SQLite message database at `.kiln/messages.db` in the project root for all inter-agent communication. Each agent has direct access via the **`kiln-db` MCP server** configured in `.claude/settings.json` and `.mcp.json`.

### Check Your Inbox

At session start and after completing any task, call the `wait_for_message` tool from the **`kiln-channel`** MCP server to receive your next handoff:

```text
wait_for_message()
```

The Channel watches the database for you and returns the message as soon as it arrives (already marked delivered). If it returns `{"received": false}` (timeout with nothing queued), call it again to keep waiting, or continue with local work and call it when you are ready.

**Do not use `read_query` to poll your inbox.** The Channel replaces manual inbox SQL.

### Send a Message (Handoff)

Use `write_query` with an INSERT. The exact SQL template is in your CLAUDE.md Runtime section. Include the complete handoff message in the `content` column.

**Priority values:**
- `0-9`: High priority (architect handoffs, critical tasks)
- `50`: Normal priority (standard handoffs and messages)
- `100+`: Low priority (informational messages)

- The project root is the directory containing `.kiln/`. From a named worktree (e.g., `.worktrees/coder/`), walk up parent directories until you find it.
- At startup, discover and remember the branch or worktree assigned to your role.
- If your assigned worktree is `@current`, `master`, or `none`, work in the main project checkout on its current branch; do not expect or create a `.worktrees/<role>` directory for that role.
- When one role has `@current` and another has a named worktree (e.g., `coordinator` with `@current` and `coder` with `coder`): the `@current` role works in the main directory on the current branch, while the named-worktree role works in `.worktrees/coder` on a sub-branch named `<current-branch>-coder`. Both roles see the same HEAD branch, but from different worktrees and branches.
- Work only in your assigned branch or worktree.
- Do not inspect, diff, merge, or base work on another branch unless that branch is specifically named in a handoff or explicit user instruction.
- Use `./tmp/` in your assigned worktree for temporary files; do not use `/tmp`.
- For handoffs, use the MCP `kiln-db` `write_query` tool to send messages directly to the database.
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

## Commit Convention

Before sending any handoff, squash all your own commits since the last merge into a single commit:

```sh
LAST_MERGE=$(git log --merges -1 --format="%H")
git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
git commit -m "[Role] Brief description - what was done"
```

**Format:** `[Role] Brief description - what was done`

Examples:
- `[Coder] Implement user registration - TDD for POST /users with email validation`
- `[Refactorer] Quality gates pass - CRAP ≤ 6, 91% coverage, DRY scan clean`
- `[Architect] Module boundaries aligned - split order_processor into command/query modules`
- `[Specifier] Accept registration story - Gherkin for email, duplicate, and empty-name cases`

Do not squash the merge commit itself — only squash your own work commits on top of it.

## Handoff Message Format

All handoff messages must include a **timestamp** for user visibility when running cycles manually. Format your handoff message as follows:

```text
Re-read your role and constitution.
Sender: <role-name>
Handoff: <specifier-handoff-name>
Branch: <branch-name>
Commit: <commit-hash>

════════════════════════════════════════════════════════════════
✓ <ROLE-NAME> HANDOFF — <timestamp in format "YYYY-MM-DD HH:MM:SS">
════════════════════════════════════════════════════════════════
<Brief description of what was accomplished>

Next role: <next-role-name>
```

**Timestamp format**: Use `YYYY-MM-DD HH:MM:SS` (e.g., `2026-06-19 14:35:22`). This allows users to see exactly when each handoff was sent.

**Visual markers**: Use box-drawing characters or dashes to make handoff messages visually distinct in the terminal, helping users track cycle progress when running manually.

**Example**:

```text
Re-read your role and constitution.
Sender: coder
Handoff: user-registration-v1
Branch: main
Commit: abc1234def5678

════════════════════════════════════════════════════════════════
✓ CODER HANDOFF — 2026-06-19 14:35:22
════════════════════════════════════════════════════════════════
TDD cycle complete: 15 tests pass, 100% acceptance, 95% coverage

Next role: refactorer
```
