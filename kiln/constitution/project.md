# Project Rules

- This project is configured for Kiln with five agents: specifier, coder, refactorer, architect, and selftest.
- Project language: Python.
- Preserve project-local Kiln configuration under `kiln/`.
- Keep swarm state local under `.ksiln/` (SQLite message queue) and worktrees under `.worktrees/`.
- Prefer terse, explicit handoffs that report state and request role-appropriate review. Do not include verifications or sender process narrative.
- Do not change another role's prompt or workflow ownership without explicit user direction.

