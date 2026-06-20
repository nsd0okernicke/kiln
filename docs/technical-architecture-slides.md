# Kiln Technical Architecture Slide Deck

---

## Slide 1: Kiln Technical Architecture

- Title: "Kiln: MCP-based Multi-Agent Development Workflow"
- Subtitle: "Technical view, agent cycle, merge strategy, and terminal orchestration"
- Presenter note: set expectations that this deck is about internal infrastructure and workflow, not product UI.

> Visual hint: simple title slide with project name and a small architecture icon or flow symbol.

---

## Slide 2: High-Level System Overview

- Kiln coordinates multiple AI agents through an MCP server and git worktrees.
- Core components:
  - `mcp-server` / SQLite inbox storage
  - Agent clients: Claude, Copilot, future Codex/Grok
  - Git branches/worktrees for isolated agent work
  - Profiles, launch scripts, and terminal layout orchestrator
- Value: deterministic agent handoff, clean agent-specific branches, and auditable merge points.

> Visual hint: a block diagram with components and arrows.

---

## Slide 3: Agent Cycle and Role Handoff

- Agents are assigned roles: specifier, coder, architect, reviewer, selftest.
- Typical cycle:
  1. `specifier` writes requirements / task context
  2. `coder` implements code in a dedicated worktree
  3. `architect` reviews structure and enforces architecture rules
  4. `reviewer` validates correctness and style
  5. `selftest` or `qa` confirms results
- Kiln uses MCP messages to route tasks and hand off work between agents.

> Visual hint: circular flow diagram labeled with roles and arrow directions.

---

## Slide 4: Worktree and Merge Strategy

- Each agent works in a separate git worktree when possible.
- Benefits:
  - isolated changes per role
  - lower conflict risk during parallel exploration
  - cleaner agent-specific history
- Merge strategy:
  - prefer squash/rebase before merging into main branch
  - preserve logical work units while avoiding noisy commit history
  - keep branch metadata for debugging and rollback
- Example: `main` ← `feature/agent-coder` ← `feature/agent-review`

> Visual hint: git branch/worktree diagram with main line and side worktrees.

---

## Slide 5: Merged Agent Instructions (`claude.md` / `copilot-instructions.md`)

- Kiln merges instruction sources into a unified agent guidance document.
- Purpose:
  - ensure Claude and Copilot receive consistent rules
  - centralize behavior expectations and workflow policies
- Key merged concepts:
  - allowed workspace operations
  - message handling and handoff protocols
  - commit/branch discipline and role definitions
- Keep the combined file as a single reference for all configured agents.

> Visual hint: two document icons merged into one, with a shared rulebook overlay.

---

## Slide 6: Terminal Layouts and Launch Workflow

- Launch scripts arrange terminals for agent sessions and shared tools.
- Typical layout:
  - left pane: active agent shell
  - right pane: git status / worktree monitor
  - bottom pane: MCP server logs and shared inbox
- Launch flow:
  1. start `Kiln`/profile script
  2. prepare agent configs
  3. launch agent clients with MCP socket path
  4. open terminals/worktrees in coordinated layout
- Value: immediate visibility into agent state, git context, and orchestration logs.

> Visual hint: terminal grid mockup with labeled panes.

---

## Slide 7: Technical Highlights and Best Practices

- Keep `profiles.yaml` and launch helpers in sync for reliable agent startup.
- Document dependency injection points: agent config, workspace root, MCP socket.
- Emphasize auditability:
  - SQLite inbox history
  - git commit metadata
  - terminal/log trace per role
- Future-proofing:
  - add Codex/Grok agent support
  - support push notifications and hybrid MCP delivery
  - centralize docs and architecture decisions in `docs/`

> Visual hint: checklist with icons for consistency, auditability, and extensibility.

---

## Slide 8: How to Use This Deck

- Use the outline to create a graphical slide deck in your preferred tool.
- Prioritize diagrams for the agent cycle and worktree/merge strategy.
- Keep wording concise and technical; let visuals carry the workflow flow.
- Add a final appendix showing team roles, file locations, and key config files.

> Visual hint: roadmap/list slide with next steps.
