# Runtime Configuration

- **Role**: `{{ROLE}}`
- **Branch**: `{{BRANCH}}` — this is the ROOT branch. Do NOT substitute your worktree sub-branch (e.g. `{{BRANCH}}-{{ROLE}}` would be wrong).
- **Worktree**: `{{WORKTREE}}`
- **Message database**: `{{DB_PATH}}` (via MCP `kiln-db` server)
- **MCP servers**: `kiln-db` (SQL read/write only — no blocking channel; message receipt is handled by the polling loop in step 1)
- **Worker delegation**: via Codex's built-in multi-agent spawn tools (`spawn_agent`/`assign_agent_task`/`wait_agent`/`close_agent`), dispatching to the `{{ROLE}}-worker` custom agent defined in `.codex/agents/{{ROLE}}-worker.toml`.
