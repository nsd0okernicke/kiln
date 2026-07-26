---
marp: true
theme: default   # or gaia / uncover
paginate: true

---

## Slide 1: Kiln Technical Architecture

- Title: "Kiln: MCP-based Multi-Agent Development Workflow"
- Subtitle: "Technical view, agent cycle, merge strategy, and terminal orchestration"
- Presenter note: set expectations that this deck is about internal infrastructure and workflow, not product UI.

> Visual hint: simple title slide with project name and a small architecture icon or flow symbol.

---

## Slide 2: High-Level System Overview

- Kiln coordinates multiple AI agents through an MCP server, SQLite messaging, and git worktrees.
- Core components:
  - `kiln-channel` MCP server — blocking `wait_for_message()` with message lifecycle tracking
  - `kiln-db` MCP server — SQL read/write for handoff messages
  - SQLite message queue (`.kiln/messages.db`) with state tracking: queued → delivered → processing → processed
  - Agent clients: Claude (thin shells + worker subagents), Copilot, future Codex/Grok
  - Git branches/worktrees for isolated agent work
  - Profiles, launch scripts, and terminal layout orchestrator
- Value: deterministic agent handoff, message recovery on timeout, clean agent-specific branches, and auditable merge points.

> Visual hint: a block diagram with components, message state machine, and arrows.

---

## Slide 3: Agent Cycle and Role Handoff

- Agents are assigned roles: specifier, coder, architect, reviewer, selftest.
- Typical cycle:
  1. `specifier` writes acceptance tests / task context
  2. `coder` implements code via TDD in a dedicated worktree
  3. `refactorer` applies quality gates (coverage, CRAP, DRY, mutation testing)
  4. `architect` reviews structure and enforces architecture rules
  5. Loop returns to specifier for next feature
- Kiln uses MCP messages to route tasks and hand off work between agents.
- Each agent runs in its own git worktree and branch (e.g., `main-coder`, `feature/ABC-refactorer`).

> Visual hint: circular flow diagram labeled with roles, branch names, and arrow directions.

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

## Slide 5: Message Lifecycle and Recovery

- Messages flow through a 4-state lifecycle in the SQLite queue:
  - **Queued**: Created by sender, waiting for receiver to pick it up
  - **Delivered**: Retrieved by receiver via `wait_for_message()`, work about to start
  - **Processing**: Receiver calls `mark_processing()`, work actively underway
  - **Processed**: Receiver calls `mark_processed()` after handoff completes
- Recovery: If an agent times out after `delivered` but before `processed`, the next cycle can pick up the message (checking both `queued` and `delivered` states)
- Benefit: Full visibility into agent work; zero message loss; testable state transitions.
- Inspection: `kiln-db.ps1 stats` shows counts per state; `wait_for_message()` logs to `.kiln/logs/channel-<role>.log`

> Visual hint: state machine diagram with queued→delivered→processing→processed transitions and recovery arrow.

---

## Slide 5a: Merged Agent Instructions (`claude.md` / `copilot-instructions.md`)

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

## Slide 6: Shell + Worker-Subagent Delegation (Phase 6)

- Claude agents in `auto`-mode roles run as **thin persistent shells** that never accumulate context.
- Each cycle, the shell:
  1. Calls `/kiln-receive` to get the next handoff (waits indefinitely if none queued)
  2. Calls `mark_processing()` to transition message to `processing` state
  3. Dispatches the `Agent` tool with `subagent_type: "<role>-worker"` (blocking)
  4. Calls `mark_processed()` after handoff completes
  5. Loops back to step 1 in the **same response**
- Worker subagent (`.claude/agents/<role>-worker.md`):
  - Gets full role + engineering + project constitution (not workflow, not MCP tools)
  - Is disposable — its full working transcript never enters the shell's context
  - Can invoke project Skills (`/tdd-red`, `/tdd-green`, `/coverage-check`, etc.)
  - Reports only final results back to shell
- Benefit: Shell CLAUDE.md stays ~140 lines through unlimited cycles; worker gets fresh context per cycle
- Validation: 8+ cycle run against LibraryHub with 50+ tests, zero stalls, zero message loss.

> Visual hint: two boxes (shell + worker) with arrows showing message in, work dispatch, result back, then loop arrow.

---

## Slide 7: Terminal Layouts and Launch Workflow

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

## Slide 8: Technical Highlights and Best Practices

- Keep agent profiles and launch helpers in sync for reliable agent startup
- Document dependency injection points: agent config, workspace root, MCP socket
- Emphasize auditability:
  - SQLite message queue with full state tracking (queued/delivered/processing/processed)
  - git commit metadata (squashed per handoff, traced in logbook.md)
  - terminal/log trace per role (channel logs + claude debug logs)
- Message queue inspection: `kiln-db.ps1 stats`, `kiln-db.ps1 list-messages <role>`, direct SQL queries
- Future-proofing:
  - Add Codex/Grok agent support
  - Support push notifications and hybrid MCP delivery
  - Bring `kiln.sh` (Unix) to parity with Phase 6 shell/worker pattern

> Visual hint: checklist with icons for consistency, auditability, extensibility, and monitoring.

---

## Slide 9: How to Use This Deck

- Use the outline to create a graphical slide deck in your preferred tool.
- Prioritize diagrams (in order of importance):
  1. Message state lifecycle (Slide 5) — the foundation of reliability
  2. Agent cycle with roles (Slide 3) — what agents do
  3. Shell + worker pattern (Slide 6) — how agents stay efficient
  4. Worktree/branch strategy (Slide 4) — isolation and merge discipline
- Keep wording concise and technical; let visuals carry the workflow
- Add a final appendix showing: team roles, file locations, key config files, and how to inspect message queue health

> Visual hint: roadmap/list slide with prioritized diagrams and next steps.

---

## Key Takeaways

- **Phase 6 is live**: Shell + worker-subagent delegation validated through 8+ cycles with 50+ tests
- **Message lifecycle ensures reliability**: Full state tracking + recovery on timeout
- **Shell context stays small**: ~140 lines through unlimited cycles (Phase 6 achievement)
- **Ready for production**: Multi-agent swarms can now run indefinitely without stalls or message loss
