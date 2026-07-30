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

## 5. Project layout — `.mcp.json` placement ✅ Decided (2026-07-30)

**Question:** Root `.mcp.json` exists for `@current` roles; worktrees get their own copy. Is root the right place long-term?

**Decision: keep it at project root, no relocation.** Every `claude` launch already passes
`--mcp-config ./.mcp.json` explicitly (`kiln.ps1:935,1182,1184`), so the location isn't
Claude-Code-imposed — but root is also Claude Code's own ecosystem-standard discovery
convention, is already correctly gitignored and cleaned up on both platforms, and `.kiln/` (the
obvious alternative) is symlinked identically into every worktree so it can't hold
per-role-differentiated content anyway.

**Found and fixed along the way — two real `kiln.sh` bugs, unrelated to placement:**

- `bin/kiln.sh`'s `prepare_agent_configs()` never wrote a root `.mcp.json` for a Claude
  `@current` role unless a Copilot agent happened to also be in the swarm — meaning the
  flagship all-Claude `default` profile (`human-in-the-loop` as `@current`) was effectively
  broken on Unix (`--mcp-config ./.mcp.json` pointed at a file that never existed).
- The same function's own comment claimed it wrote `~/.copilot/mcp-config.json` (matching
  `kiln.ps1`'s `Prepare-AgentConfigs`, the location Copilot CLI actually reads), but the body
  never did — it wrote an incomplete file to the project root instead. Copilot agents likely had
  no working MCP config on Unix either.

Both fixed by rewriting `prepare_agent_configs()` to mirror `kiln.ps1`'s `Write-ClaudeConfig` +
`Prepare-AgentConfigs` almost line-for-line. Still needs a live Unix/WSL run to confirm end to
end (no zsh available to test in the environment this was fixed from).

**Found and fixed later (2026-07-30) — unrelated `kiln.ps1`-only bug, same neighborhood:**
`Prepare-Workspace` called `Write-DirectoryGitignore $KILN_DIR`, writing a blanket `*`
`kiln/.gitignore` into the *version-controlled* `kiln/` directory (should only ever apply to
`.kiln/`/`.worktrees/`) — this had been noticed once before mid-session and worked around locally
by deleting the stray file, without fixing the generator. It then caused real damage in a live
run: `kiln/project/` (constitution, roles, skills) was never git-tracked, so every new worktree
(`git worktree add`) came up with **no `kiln/` directory at all**. Worker-agent prompts still
came out correct (`Write-GeneratedWorkerAgent` reads `kiln/project/` straight off disk, not via
git, at launch time), so this hadn't broken agent behavior yet — but `kiln/project/` changes could
never be committed or merged going forward. Fixed by removing the `Write-DirectoryGitignore
$KILN_DIR` call entirely and adding a self-heal step: if a project still carries the generated
file (exact fingerprint — a lone `*`), `Prepare-Workspace` now deletes it on next launch.
`kiln.sh` never had this bug (no per-directory `kiln/.gitignore` writer exists there). Verified:
fresh `-Init` no longer creates the file (`kiln/` shows as plain untracked, not ignored, in
`git status`); isolated repro confirmed the self-heal removes a pre-existing `*`-only file.

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

- [ ] Spike: map `kiln.ps1` / `kiln.sh` surface area (including the folded-in `-Init`/`init` scaffolding, see §8) and shared vs divergent logic
- [ ] Choose approach:
  - **A.** Stay dual-shell; extract shared logic aggressively
  - **B.** Python core CLI (`kiln` entrypoint) calling thin terminal adapters
  - **C.** Hybrid — Python for init/generate/MCP helpers; shells only for terminal launch
- [ ] If B/C: minimal vertical slice (e.g. `kiln init` + profile load + dry-run generate) before full port
- [ ] Compatibility: keep `bin/kiln.ps1` / `kiln.sh` as shims during migration

---

## 8. Unify `kiln-init` into main CLI ✅ Done (2026-07-30)

**Goal:** One entrypoint for users — `kiln init` / `.\kiln.ps1 init` instead of separate `kiln-init.ps1` / `kiln-init.sh`.

**Done, stayed dual-shell** (no Python CLI — that's still the separate, undecided §7): `kiln.ps1 -Init -WorkingDir <path>` / `kiln.sh init <path>` now scaffold a project directly. `-Target` works as a backward-compatible alias for `-WorkingDir` on the PS side. `kiln-init.ps1`/`kiln-init.sh` briefly existed as thin deprecated wrappers, then were removed outright (2026-07-30) once README/docs no longer referenced them — calling them directly no longer works, use `kiln.ps1 -Init`/`kiln.sh init`.

**PS positional-arg bug found and fixed post-merge:** `kiln.ps1 init -WorkingDir X` (Unix-style syntax tried on the PS side) silently bound the stray `init` token to `$Terminal` instead of erroring, because PowerShell assigns unclaimed positional tokens to the next parameter in declaration order. Fixed by adding an explicit `$Command` parameter as the first declaration (claims position 0): `"init"` is now treated as `-Init`, anything else errors clearly instead of silently mis-binding. (A first attempt at fixing this via `[CmdletBinding(PositionalBinding=$false)]` introduced a *second* bug — it auto-adds a built-in `-Debug` common parameter that collides with kiln.ps1's own `-Debug` switch — reverted in favor of the declaration-order approach, which needs no `CmdletBinding`.)

Folded in **as-is**, bugs and parity gaps included (left for a separate pass, likely folded into §6): Bash's init still lacks a `-NoGit` equivalent and still requires the target not to already exist; Bash still copies an extra `kiln/.mcp.json` framework file PS doesn't; PS/Bash still default the (inert) `-Profile`/`--profile` flag differently (`default` vs `dev`).

Functionally verified end-to-end on Windows (fresh scaffold, `-Target` alias, `-Example library-hub`, `-NoGit`, `-ListProfiles`, and the deprecated wrapper all tested against real output). Bash/zsh side verified by syntax-check only (no zsh available in the environment this was built in) — worth a real run on Unix/WSL.

~~### Work~~

- [x] Design subcommand surface: `-Init` (PS switch) / `init` (Bash leading positional subcommand) — `stop`/`doc-*`/cleanup helpers deliberately not addressed here
- [x] Fold scaffold logic from `kiln-init.*` into main scripts
- [x] Preserve flags (`-Target` / path, `-Example` / `--example`)
- [x] Deprecate standalone init scripts with a thin wrapper
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

## 10. New example projects: LibraryHub (Java/Spring) and Battlezone ✅ Done (2026-07-30)

**Goal:** Add two new reference examples under `examples/`, following the same pattern as
`examples/library-hub/README.md` — each needs a full project brief plus a matching
`kiln/project/` starter set (`constitution/project.md`, `constitution/engineering.md`, and any
other project-specific role/constitution tweaks), not just a spec document.

### 10.0 Prerequisite — generalize the example-brief copy logic ✅ Done (2026-07-30)

**Decided (2026-07-30):** `examples/<name>/kiln/project/constitution/` is the right home for
per-example `project.md`/`engineering.md` (this already matches the existing
`examples/library-hub/kiln/project/constitution/project.md`). But `Copy-KilnInitExampleBrief`
(`bin/kiln.ps1:1345`) and `init_copy_example_brief` (`bin/kiln.sh:115`) are both hardcoded to the
literal string `"library-hub"` — even the README copy step is gated behind
`if ($ExampleName -ne "library-hub") { return }` — so neither new example would work until this
is fixed, regardless of which one lands first.

**Decision: generalize to copy the whole directory.** Drop the hardcoded name check; copy every
file present in `examples/<name>/kiln/project/constitution/` (not just `project.md`) over the
scaffolded defaults, so `engineering.md` (and anything else an example needs to override) is
picked up automatically without another code change per example.

- [x] `bin/kiln.ps1`: removed the `"library-hub"` literal check in `Copy-KilnInitExampleBrief`;
  replaced the single hardcoded `project.md` copy with a loop over all files in
  `examples/<name>/kiln/project/constitution/`. Also added a yellow warning (not a hard error) if
  `-Example <name>` points at a directory that doesn't exist under `examples/`, instead of
  silently doing nothing.
- [x] `bin/kiln.sh`: same change in `init_copy_example_brief`, same warning-not-error behavior
- [x] Verified: fresh `-Example library-hub-java` scaffold on Windows actually copies README.md,
  the Maven-flavored `project.md`, *and* `engineering.md` (previously never copied for any
  example, including the original `library-hub` — it happened to not matter there since the
  framework's scaffolded default `engineering.md` is already Python-flavored). Re-verified
  `-Example library-hub` still produces byte-identical output to before the change (no
  regression), and an unknown example name (`-Example totally-not-real`) now warns and continues
  instead of erroring. Bash/zsh side verified by syntax-check only (`bash -n`, patched around the
  one pre-existing zsh-only `<->` construct) — no zsh available in this environment to run live.

### 10.1 LibraryHub — Java/Spring variant ✅ Done (2026-07-30)

**Decided:** reuse the Python LibraryHub's domain, bounded contexts, and user stories **1:1**
(same CAT-1..6 / LOAN-0..5, same two-service split) so the two examples stay directly comparable
— but let package layout and DI diverge to be idiomatic Java/Spring rather than a literal
transliteration: multi-module Maven build (parent POM + `catalog-service`/`loans-service`
modules) with a standard `src/main/java/...` tree, instead of the Python original's flat
no-`src/`-wrapper layout.

- [x] New example dir `examples/library-hub-java/` — same domain/user stories as the existing
  Python LibraryHub, ported to Java/Spring Boot
- [x] `README.md` brief: architecture, bounded contexts, user stories, layering rules, tech stack,
  quality gates, testing strategy (mirrors the structure of the existing Python brief)
- [x] `project.md` / `engineering.md` — Maven multi-module build (parent POM), Spring Boot 3.x
  (MVC, not WebFlux), Spring Data JPA with PostgreSQL, Spring AMQP with RabbitMQ, JUnit 5 with
  Cucumber-JVM, Testcontainers, and jqwik, JaCoCo coverage, PIT mutation/CRAP (threshold 30,
  matches `skill-orchestration.md`'s Java/Kotlin row), Checkstyle/Spotless, ArchUnit layering
- [x] **§10.0** generalization landed — `-Example library-hub-java` now scaffolds correctly
- [x] End-to-end smoke test: `kiln.ps1 -Init -Example library-hub-java` on Windows, confirmed
  README.md, `project.md`, and `engineering.md` all land with this example's Maven/Spring content
  (not the framework's Python defaults); `-Example library-hub` regression-checked unaffected

### 10.2 Battlezone (1980 Atari) — something totally different ✅ Done (2026-07-30)

**Decided (stack):** Python 3.10+ + `pygame` (SDL2-backed — confirmed cross-platform on Windows
and Linux, no platform-specific code needed). Chosen over a TypeScript/Canvas rewrite because it
reuses the framework's existing Python tool table as-is (`pytest`, `mutmut`, `radon`, `mypy`) —
the "totally different" axis is the domain (real-time game vs CRUD service) and architecture
(fixed-timestep loop vs request/response), not a third from-scratch toolchain.

**Decided (architecture fit):** kept the same 3-layer `infrastructure → application → domain`
dependency discipline as `library-hub`, just remapped — `infrastructure/` is pygame
(window/input/rendering) instead of HTTP/DB, `domain/`+`application/` is the pure, fully
testable simulation core (movement, collision, AI state machine, 3D→screen projection math,
scoring). `GameSession.tick()` → `FrameState` is the one boundary object between the testable
core and the environment-bound shell — no `pygame` import ever crosses into `domain/` or
`application/`.

**Decided (testing for a game):** unit + Hypothesis property tests + headless `pytest-bdd`
acceptance tests all run directly against `domain/`/`application/` with zero pygame involvement
(no window ever opens in CI/agent runs). `infrastructure/` (the actual window, input polling,
drawing) has no automated gate — it's verified by a manual playtest step before any handoff that
touches it, called out explicitly in both `README.md` and `project.md`.

- [x] New example dir `examples/battlezone/` — game implementation, not a CRUD service; stack and
  rendering approach (hand-rolled wireframe projection, no 3D engine) decided and documented above
- [x] `README.md` brief: game mechanics (TANK-#/AI-#/WORLD-#/GAME-#/HUD-# user stories), explicit
  MVP scope (tanks only, no saucers/sound/persistence/networking — see Out of Scope), architecture
  (game loop, rendering, input, collision), out-of-scope list
- [x] `project.md` / `engineering.md` — Python/pygame tech stack, quality gates explicitly scoped
  to `domain/`+`application/` only (mutation ≥80%, coverage >90%, strict mypy), `infrastructure/`
  excluded and covered by manual playtest instead; `engineering.md` adds the pygame/headless-test
  notes (`SDL_VIDEODRIVER=dummy` if ever needed, the "never import pygame in domain/application"
  rule) the framework's generic Python default doesn't know about
- [x] Sanity-checked `kiln/project/roles/coder.md` and `constitution/workflow.md` — neither
  hardcodes anything HTTP/REST/DB-specific (`coder.md`'s "HTTP routers, DB models" is illustrative
  phrasing, not an enforced assumption); `constitution/engineering.md`'s existing "environmentally
  unsuitable modules that open GUIs... or hang under automated tests" rule already anticipated
  exactly this case. **No changes needed to role files or `workflow.md`.**
- [x] End-to-end smoke test: `kiln.ps1 -Init -Example battlezone` on Windows, confirmed README.md,
  `project.md`, and `engineering.md` all land with this example's content

---

## Suggested order

1. ~~**§4** Slide deck refresh (docs-only, unblocks presentations)~~ — done
2. **§1.1–1.2** Codex live validation + mixed-agent confidence
3. **§6** Unix parity (or pair with §7 if choosing Python)
4. **§3** Skills hardening (quality of autonomous runs)
5. ~~**§10** New example projects (LibraryHub Java/Spring, Battlezone)~~ — done
6. ~~**§5 / §8** Layout + CLI ergonomics~~ — done
7. **§2** Documentation MCP (net-new capability)
8. **§7** Full Python port only if dual-shell cost stays high after smaller extractions
9. **§9** Local traffic proxy (measure first, then optimize tokens)
10. **§1.3** Grok when unblocked
