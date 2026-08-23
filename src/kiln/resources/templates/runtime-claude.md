# Runtime Configuration

- **Role**: `{{ROLE}}`
- **Branch**: `{{BRANCH}}` — this is the ROOT branch. Do NOT substitute your worktree sub-branch (e.g. `{{BRANCH}}-{{ROLE}}` would be wrong).
- **Worktree**: `{{WORKTREE}}`
- **Message database**: `{{DB_PATH}}` (via MCP `kiln-db` server)
- **MCP servers**: `kiln-db` (SQL read/write) · `kiln-channel` (blocking message receiver)
