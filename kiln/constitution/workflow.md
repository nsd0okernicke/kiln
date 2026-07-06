# Workflow Rules

## Message Queue

Kiln uses a SQLite message database at `.kiln/messages.db` in the project root for all inter-agent communication. Each agent has direct access via the **`kiln-db` MCP server** configured in `.claude/settings.json` and `.mcp.json`.

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
- For handoffs, the underlying mechanism is the MCP `kiln-db` `write_query` tool: Claude agents send it via `/kiln-handoff` (which calls `write_query` internally); Copilot agents call `write_query` directly per their loop instructions.
- Start every handoff message with: `Re-read your role and constitution.`
- The specifier invents a short, stable handoff name for each accepted specification handoff.
- Every later handoff for that work must include the specifier handoff name.
- Handoffs must report only essential state, not prescribe process. After the opening line, include exactly these fields and no other prose: sender role, specifier handoff name, branch name, and commit hash.
- Do not tell the receiving role how to do its job, repeat your process, or ask it to continue sender-owned responsibilities. The normal request is: `Apply your own role rules to this state.`
- When receiving a handoff, ignore sender process narrative and decide next actions only from your own role prompt, the constitution, and the current project state.
- If the expected git layout or assigned worktree is missing, stop and report instead of silently working in the wrong place.

## Commit Convention

Before sending any handoff, squash all your own commits since the last merge into one commit (the exact git commands are provided in your handoff steps — `/kiln-handoff` for Claude agents, the loop's squash step for Copilot agents).

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

## Handoff Routing

| Role | Sends to |
| ---- | -------- |
| specifier | coder |
| coder | refactorer |
| refactorer | architect |
| architect | specifier |
| selftest | selftest |
