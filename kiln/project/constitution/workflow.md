# Workflow Rules

## Message Queue

Kiln uses a SQLite message database at `.kiln/messages.db` in the project root for all inter-agent communication. Each agent has direct access via the **`kiln-db` MCP server** configured in `.claude/settings.json` and `.mcp.json`.

**Priority values:**

- `0-9`: High priority (architect handoffs, critical tasks)
- `50`: Normal priority (standard handoffs and messages)
- `100+`: Low priority (informational messages)

**Worktree & Branch:**
- Work only in your assigned branch or worktree (as shown in Runtime Configuration).
- Do not inspect, diff, merge, or base work on another branch unless specifically named in a handoff or explicit user instruction.
- Use `./tmp/` in your assigned worktree for temporary files; do not use `/tmp`.

**Handoff Mechanics:**
- For handoffs, the underlying mechanism is the MCP `kiln-db` `write_query` tool: Claude agents send it via `/kiln-handoff` (which calls `write_query` internally); Copilot agents call `write_query` directly per their loop instructions.
- The specifier invents a short, stable handoff name for each accepted specification handoff.
- Every later handoff for that work must include the specifier handoff name.
- Handoffs must report only essential state, not prescribe process. Include exactly these fields and no other prose: sender role, specifier handoff name, branch name, and commit hash (see Handoff Message Format template).
- Do not tell the receiving role how to do its job, repeat your process, or ask it to continue sender-owned responsibilities. The normal request is: `Apply your own role rules to this state.`
- When receiving a handoff, ignore sender process narrative and decide next actions only from your own role prompt, the constitution, and the current project state.
- If the expected git layout or assigned worktree is missing, stop and report instead of silently working in the wrong place.

## Commit Convention

Before sending any handoff, squash all your own commits since the last merge into one commit (the exact git commands are provided in your handoff steps — `/kiln-handoff` for Claude agents, the loop's squash step for Copilot agents).

**Format:** `{{COMMIT_FORMAT}}`

Do not squash the merge commit itself — only squash your own work commits on top of it.

## Handoff Message Format

All handoff messages must include a **timestamp** for user visibility when running cycles manually. Format your handoff message as follows:

```text
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
