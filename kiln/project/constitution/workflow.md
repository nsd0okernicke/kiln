<!-- Copied into <project>/kiln/project/constitution/workflow.md during project init (kiln.ps1 -Init / kiln.sh init). Framework default handoff protocol — customize only if your project's workflow genuinely differs. -->

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
- For handoffs, the underlying mechanism is the MCP `kiln-db` `query` tool: Claude agents send it via `/kiln-handoff` (which calls `query` internally); Copilot agents call `query` directly per their loop instructions. `human-in-the-loop` may instead send via the `kiln send` CLI (`kiln/framework/scheduler/send.py`), which performs the same INSERT without going through MCP.
- A role opted into the deterministic scheduler (`"scheduler": "python"` in the profile, e.g. `role_scheduler.py`) or watched by an `inbox` pane (`"scheduler": "inbox"`, e.g. `kiln inbox` for `human-in-the-loop`) receives and merges handoffs outside any LLM session entirely — `/kiln-receive` does not apply there. See the role's own file and `kiln/framework/scheduler/` for specifics.
- The specifier invents a short, stable handoff name for each accepted specification handoff.
- Every later handoff for that work must include the specifier handoff name.
- Handoffs must report only essential state, not prescribe process. Include exactly these fields and no other prose: sender role, specifier handoff name, branch name, and commit hash (see Handoff Message Format template).
- Do not tell the receiving role how to do its job, repeat your process, or ask it to continue sender-owned responsibilities. The normal request is: `Apply your own role rules to this state.`
- When receiving a handoff, ignore sender process narrative and decide next actions only from your own role prompt, the constitution, and the current project state.
- If the expected git layout or assigned worktree is missing, stop and report instead of silently working in the wrong place.

## Commit Convention

Before sending any handoff, squash all your own commits since the last merge into one single, human-readable commit (the exact git commands are provided in your handoff steps — `/kiln-handoff` for Claude agents, the loop's squash step for Copilot agents).

The squash commit must begin with the agent role in brackets and then describe the outcome of the work, for example `[Specifier] Add acceptance criteria for order intake`, `[Coder] Implement order creation via TDD`, `[Refactorer] Improve coverage and remove duplication`, `[Architect] Validate module boundaries and mutation safety`, or `[Human-in-the-loop] Hand off approved request for specification`.

Use concise, imperative wording. Do not use merge-only or log-only messages, and do not squash the merge commit itself — only squash your own work commits on top of it.

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

When you have nothing to merge (a human's opening request, a ping), leave `Commit:` **empty** — do not write `none`, `n/a`, or an explanation. Only a git hash in that field makes the receiver merge.

## Handoff Routing

| Role | Sends to | When Sender |
| ---- | -------- | ----------- |
| human-in-the-loop | specifier | |
| specifier | coder | |
| specifier | human-in-the-loop | architect |
| coder | refactorer | |
| refactorer | architect | |
| architect | specifier | |

The optional third column makes routing depend on who sent the inbound handoff. A row whose
`When Sender` matches wins; the row with a blank `When Sender` is the role's default.

This is what closes the cycle. Without the `specifier | human-in-the-loop | architect` row,
an architect's completed-cycle report reaches the specifier and is routed straight back to
`coder` — the swarm loops forever instead of returning to the human. The condition used to
live only as prose in `roles/specifier.md`, which meant only an LLM could act on it; as a
table row it is data that the scheduler follows too.

`roles/specifier.md` → "Auto-Mode Worker Entry Point" still governs *what the specifier does*
with such a message (forward it as-is, do not re-run the Gherkin workflow). Only the routing
decision moved here.
