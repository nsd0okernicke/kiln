# Kiln Development Plan

---

## 1. Agent Backend Completion (Codex, Grok)

**Goal:** Finish multi-backend support so profiles can mix Claude, Copilot, and Codex reliably; keep Grok deferred until a viable CLI exists.

### Status

| Backend | Launch + MCP config | Worker delegation | Skills wiring | Full multi-cycle swarm |
|---|---|---|---|---|
| Claude | done | done (live-validated) | done | done |
| Copilot | done | done (CLI-confirmed) | done | not fully multi-cycle |
| Codex | done (generation validated) | implemented, not live-spawn tested | not wired | not done |
| Grok | blocked | — | — | — |

**Grok blocker (2026-07-28):** the `grok` CLI on PATH is third-party (`grok-cli-hurry-mode`), not official xAI. Persistent session has no unattended auto-approve path; only one-shot `-p` auto-approves (incompatible with Kiln's persistent wait loop without a poll-and-relaunch redesign). Revisit if an official CLI appears or poll-and-relaunch becomes worth building.

### 1.1 Codex — remaining work

- [ ] **Live multi-cycle validation** — full swarm run (WezTerm/WT on Windows; tmux on Unix once parity exists). Confirm `spawn_agent` / `assign_agent_task` / `wait_agent` / `close_agent` sequence in a real logged-in session; generation-only checks are already done.
- [ ] **Skills wiring** — investigate Codex project-level skills discovery (`~/.codex/skills/` exists; project path/format unverified). Wire into `Prepare-Skills` / `prepare_skills` if feasible; document if not.
- [ ] **Profile** — keep/use `codex-test` (or equivalent) for isolated validation.

### 1.2 Mixed-agent testing

- [ ] Profile: Claude + Copilot + Codex (and document any agents that cannot coexist)
- [ ] Cross-backend handoffs (message routing, branch context, delivery timing)
- [ ] README: setup notes, mixed-agent example profiles, agent-specific limitations

### 1.3 Grok (deferred)

- [ ] Re-evaluate when an official / unattended-capable Grok CLI exists
- [ ] Or design a generic one-shot poll-and-relaunch backend (would also cover other non-persistent CLIs)

---

## 2. Documentation MCP Server

**Goal:** MCP server that indexes and serves project/external docs so agents can search them at runtime (specifier, architect, and others).

**Sources (target):** local PDFs, URLs, Markdown (local/git), OpenAPI/GraphQL schemas; Confluence/Notion exports if available.

### Work

- [ ] **Schema** — tools: `search_documentation(query, source?, max_results?)`, `get_document(id)`, `list_sources()`; resource types for pdf/url/markdown/schema; metadata (title, author, date, version, tags)
- [ ] **Indexer** — PDF extract, URL fetch + cache, Markdown hierarchy, schema → readable docs; optional semantic search (embeddings)
- [ ] **Server** — `kiln/framework/mcp-server/doc-server.py`; register with existing MCP setup; cache in `.kiln/docs.db`
- [ ] **Config** — optional `documentation` list on profiles (path/url + type)
- [ ] **Role integration** — expose to specifier/architect (and document in constitution/workflow)
- [ ] **CLI** — `kiln doc-index` / `doc-search` / `doc-sources` (or PowerShell/sh equivalents)
- [ ] **Tests** — mixed PDF layouts, rate-limited URLs, relevance, large collections

---

## 3. Skills Hardening (remaining from audit)

**Goal:** Close gaps left after the 2026-07-29 skills audit. Orchestration doc is done; `acceptance-test-writer` removed.

### Open items

- [ ] **Tool preconditions** — role startup checks for external tools (coverage, radon, PIT, gherkin-mutator, etc.); clear “missing tool” failures / graceful skip
- [ ] **`zoom-out`** — document real invocation path, or remove if obsolete (`disable-model-invocation: true` today)
- [ ] **Mutation-testing ownership** — architect owns acceptance mutation; shared manifest format; handoff protocol between refactorer and architect
- [ ] **Tool version pins** — minimum versions in `constitution/engineering.md` (radon, coverage.py, PIT, gherkin-mutator, …)
- [ ] **`property-test-generator` entry point** — when/why in refactorer quality-gate sequence
- [ ] **Error paths** — exercise “tool unavailable” branches in refactorer/architect workflows

---

## 4. Update technical slide deck

**Goal:** Refresh `docs/technical-architecture-slides.md` so it matches the product as described in **README.md** and the current diagram set under `docs/diagrams/` + `docs/images/`.

Slim update only — no new architecture research.

### Sources of truth

- **README.md** — default profile (human-in-the-loop + autonomous cycle), wrapper/worker model, backends, message lifecycle, worktrees, terminal layouts
- **Diagrams** — `docs/diagrams/*.mmd` and rendered `docs/images/` (e.g. coder-internal-cycle, wrapper-worker, topology, kiln1–4). Drop references to deleted assets (old agent-cycle/message-lifecycle/worktree/wezterm/wt screenshots where removed).

### Work

- [ ] Align slide list and wording with README (default topology, `/kiln-receive` → work → `/kiln-handoff`, Codex status, Known Limitations)
- [ ] Point slides at current images/diagrams; fix broken image links
- [ ] Trim obsolete Phase labels / outdated backend claims
- [ ] Keep bullets short and presentation-ready

---

## 5. Project layout — `.mcp.json` placement

**Question:** Root `.mcp.json` exists for `@current` roles; worktrees get their own copy. Is root the right place long-term?

### Investigate / decide

- [ ] Document current generation rules (root vs per-worktree, Claude vs other backends)
- [ ] Decide target layout (e.g. keep root for `@current` only; never commit generated files; or move template under `kiln/framework/`)
- [ ] Align `.gitignore`, cleanup scripts, and README project-structure section
- [ ] Implement if the decision moves files or generation paths

---

## 6. Unix / `kiln.sh` parity

**Goal:** Bring the Unix launcher to feature parity with `kiln.ps1` so the receive → delegate → handoff loop and template injection work on macOS/Linux.

### Context (from README Known Limitations)

- `kiln.sh` does not inject loop/runtime templates for Claude/Copilot the way Windows does
- No full `auto`/`manual` mode parity for those backends on Unix
- Codex path on `kiln.sh` was built closer to parity (hand-assembled wrapper/worker), but still needs live validation

### Work

- [ ] Inventory gaps vs `kiln.ps1` (templates, skills, MCP configs, worker generation, status bar, cleanup)
- [ ] Port loop/runtime template injection and mode handling
- [ ] Live multi-cycle test on Unix (tmux + WezTerm/Terminal.app)
- [ ] Document any remaining platform differences in README

---

## 7. Launcher language — keep dual shell vs Python

**Idea:** Replace (or wrap) PowerShell + zsh with a single Python CLI for maintainability and parity.

### Decide first

- [ ] Spike: map `kiln.ps1` / `kiln.sh` / `kiln-init.*` surface area and shared vs divergent logic
- [ ] Choose approach:
  - **A.** Stay dual-shell; extract shared logic aggressively
  - **B.** Python core CLI (`kiln` entrypoint) calling thin terminal adapters
  - **C.** Hybrid — Python for init/generate/MCP helpers; shells only for terminal launch
- [ ] If B/C: minimal vertical slice (e.g. `kiln init` + profile load + dry-run generate) before full port
- [ ] Compatibility: keep `bin/kiln.ps1` / `kiln.sh` as shims during migration

---

## 8. Unify `kiln-init` into main CLI

**Goal:** One entrypoint for users — `kiln init` / `.\kiln.ps1 init` instead of separate `kiln-init.ps1` / `kiln-init.sh`.

### Work

- [ ] Design subcommand surface: `init`, (future) `stop`, `doc-*`, cleanup helpers
- [ ] Fold scaffold logic from `kiln-init.*` into main scripts (or into Python CLI if §7 lands first)
- [ ] Preserve flags (`-Target` / path, `-Example` / `--example`)
- [ ] Deprecate standalone init scripts with a thin wrapper or clear migration note in README
- [ ] Update Quick Start docs

---

## 9. Local proxy for agent traffic (token optimization)

**Goal:** Run a local proxy that agents' API traffic flows through, so we can monitor request/response sizes and patterns and use that data to optimize token usage (prompt bloat, redundant context, skill/payload size, cycle cost per role).

**Reference:** [@mattpocockuk](https://x.com/mattpocockuk) on proxying Claude Code to inspect what actually hits the model, then strip system-prompt bloat (down to ~13K tokens startup) — [tweet, 2026-07-07](https://x.com/mattpocockuk/status/2074464823232888987). Same idea: measure via proxy first, then cut.

### Why

- Multi-agent cycles multiply cost; today we lack a per-role, per-cycle view of tokens
- Wrapper/worker, skills, constitution merge, and handoff payloads are all candidate hotspots — need measurement before cutting

### Work

- [ ] **Spike proxy approach** — MITM-style local proxy vs vendor-specific hooks (Claude/Copilot/Codex env vars for base URL / HTTP proxy); pick what each backend actually supports
- [ ] **Stand up local proxy** — capture request/response metadata (model, role, timestamps, input/output token estimates or byte sizes); persist under `.kiln/` (e.g. traffic log / SQLite)
- [ ] **Wire agent launch** — point generated agent configs/env at the proxy when enabled (profile flag or `kiln.ps1`/`kiln.sh` switch); document TLS/cert trust if needed
- [ ] **Dashboards / reports** — per-role and per-cycle summaries: tokens in/out, largest prompts, skill/tool call volume; enough to find optimization targets
- [ ] **Optimization loop (follow-on)** — use measured data to slim templates, skills, constitution injection, and handoff payloads; re-measure after changes
- [ ] **Privacy / safety** — redaction options (no full prompt bodies by default if sensitive); local-only by default; never log secrets

### Notes

- Observability first; cutting tokens is a second phase driven by proxy data
- May interact with §7 (Python CLI) if the proxy ships as a small local service started by the launcher

---

## Suggested order

1. **§4** Slide deck refresh (docs-only, unblocks presentations)
2. **§1.1–1.2** Codex live validation + mixed-agent confidence
3. **§6** Unix parity (or pair with §7 if choosing Python)
4. **§3** Skills hardening (quality of autonomous runs)
5. **§5 / §8** Layout + CLI ergonomics
6. **§2** Documentation MCP (net-new capability)
7. **§7** Full Python port only if dual-shell cost stays high after smaller extractions
8. **§9** Local traffic proxy (measure first, then optimize tokens)
9. **§1.3** Grok when unblocked
