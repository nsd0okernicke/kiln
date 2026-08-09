# Unified Python core: deterministic auto-mode scheduler + Python launcher (merges #5 and #7)

*Draft issue body — supersedes standalone issues #5 ("Launcher language — keep dual shell vs Python") and #7 ("Plan: Replace auto-mode wrapper LLM sessions with a deterministic Python scheduler"). Intended to replace #7's body and close #5 with a pointer here.*

## Goal

Replace the auto-mode wrapper LLM session with a deterministic Python scheduler process, **and** consolidate Kiln's launcher core on Python at the same time, as one architecture rather than two separately-reconciled efforts. The two were previously scoped as independent issues; investigation found no structural contradiction between them, but a real duplication risk (routing/DB logic reimplemented once in PowerShell for the legacy wrapper path and again in Python for the scheduler) if planned separately — see "Why merged" below.

## Why merged

- Issue #7's scheduler design already needs its own Python implementation of routing-table parsing, message DB operations, worker prompt construction, and a status-sentinel contract. Issue #5 separately asked whether `kiln.ps1`/`kiln.sh` should become Python. Building #7 without resolving #5 first means shipping a second, divergent implementation of routing/DB logic (Python `routing.py`/`db.py` for the scheduler, PowerShell `Read-HandoffRoutingTable` etc. for everything else) — an avoidable duplication, not a fundamental one.
- `swarm-forge` (Uncle Bob's comparable tool, `github.com/unclebob/swarm-forge`) validates the "single implementation language, thin per-platform launch shims" shape for this exact class of problem: it's built entirely in Babashka (Clojure scripting) with zsh reduced to 4-line `exec bb ...` wrappers — one language for all real logic, platform-specific code only for opening terminal windows. Kiln should follow the same shape, in Python.
- Note: swarm-forge is architecturally *closer to Kiln's current wrapper pattern* than to this issue's proposed scheduler — each of its panes still runs a persistent, interactive LLM session, not a one-shot subprocess call. It pushes more mechanics into deterministic code than Kiln's current design (e.g. an explicit `done_with_current.sh` call instead of free-text judgment of completion), which supports this issue's general direction, but it does not itself prove out the one-shot-subprocess design below — that remains genuinely novel and still needs the spike in this issue.

## Current status quo (context)

Every auto-mode role (`coder`, `refactorer`, `architect`, and `specifier` when acting as an auto-mode worker entry point) runs as a persistent LLM CLI session — a "wrapper" — inside a terminal pane, following a fixed prompt template (`kiln/framework/templates/loop-auto-{claude,copilot,codex}.md`). That wrapper's job is almost entirely mechanical already:

- Polling `.kiln/messages.db` (`kiln/framework/mcp-server/channel.py:59-198` — pure SQL + sleep loop), marking `delivered`/`processing`/`processed`.
- Extracting fixed fields (`Sender:`, `Handoff:`, `Branch:`, `Commit:`) from a rigid message template (`kiln/project/constitution/workflow.md:37-53`) and `git merge`-ing.
- Looking up the next hop in a routing table, already **data-driven** — `Read-HandoffRoutingTable` (`bin/kiln.ps1:787-800`) is a generic markdown-table parser reading `workflow.md`'s `## Handoff Routing` section, not hardcoded in code. The one non-data-driven exception: the specifier's conditional override (route to `human-in-the-loop` instead of `coder` when `Sender: architect`) lives only as LLM-readable prose in `roles/specifier.md`, because the table format has no way to express "when sender is X" — see the `workflow.md` change below, which fixes this as part of this issue rather than inventing a parallel Python-only override table.
- Squash-committing and formatting/inserting the outbound handoff message.

Two things aren't mechanical today: composing the worker's task prompt (trivially templatable), and judging the worker's free-text final report as done vs. blocked (no structured status field exists).

Critically, `spawn_agent`/`assign_agent_task`/`wait_agent`/`close_agent` (Codex) and Copilot's delegation are **not kiln code** — they're opaque in-session vendor mechanisms (Codex's are undocumented CLI internals; Copilot's is explicitly documented as "the model's own judgment call," not a reliable API). Trying to drive those from outside a live session is a dead end. The proposal below sidesteps this entirely: instead of asking a wrapper LLM to spawn an in-session sub-agent, a deterministic Python process launches the worker as a **fresh one-shot CLI process** per task (`claude -p`, `codex exec`, `copilot -p` — exact flags need a spike). This works uniformly across all three backends and never touches the vendor spawn mechanisms.

**Scope decision (unchanged from original #7):** manual-mode roles (`specifier`, `human-in-the-loop`) stay as real interactive LLM sessions — they hold actual conversations and require explicit human approval each cycle, which isn't scriptable. Only auto-mode delegating wrapper roles are converted.

**Worker report contract decision (unchanged):** replace the worker's free-text final report with a structured sentinel — the worker's last line of output must be `KILN-STATUS: done` or `KILN-STATUS: blocked` plus a one-line summary. Small, centrally-generated addition to the worker prompt, not a change to each `roles/*.md`.

## How pane creation actually works today (relevant to both halves of this merge)

- **WezTerm**: `kiln.ps1` does not create panes directly. `Start-WezTermSession` (`lib/terminal-adapters/wezterm.ps1:41-88`) writes a generated Lua config to `~/.wezterm.lua` plus role/layout data as JSON env vars, then runs `wezterm.exe start`. WezTerm's own embedded Lua `gui-startup` handler does the actual `mux.spawn_window`/`split`/`pane:send_text(cmd)` work (`wezterm.ps1:259-388`) — pane creation is a long-lived responsibility inside the WezTerm process itself, not the outer launcher.
- **Windows Terminal**: simpler — `kiln.ps1` builds a `wt.exe new-tab ... pwsh -NoExit -Command <agentCmd>` argument list directly (`kiln.ps1:1047-1141`, `1813-1818`), no intermediate config file.
- **Pane lifecycle**: no supervisor exists (`Test-TerminalWindowExists`/`Test-TerminalTracksWindows` both hardcoded `$false`, `wezterm.ps1:14-21`). The pane hosts a persistent shell; the agent CLI is injected as a command and, on exit, the pane reverts to an idle prompt. A persistent Python scheduler process is a legitimate drop-in for "the thing injected into the pane."
- Today's wrapper session also manages stdio MCP subprocesses declared in `.mcp.json` (`kiln.ps1:589-611`) and TTY-interactive behavior (approval prompts, skills discovery). The scheduler design below deliberately routes around all of this instead of replicating it — it talks to `messages.db` directly and invokes workers as one-shot subprocesses, so it needs none of the MCP-management machinery. This is a redesign, not a naive command-string swap.
- Python already exists inside the framework today (`kiln/framework/mcp-server/*.py`, `kiln/framework/tools/set-status.py`, an embedded `python -c` heredoc in `kiln.ps1` for DB init at `kiln.ps1:1690-1727`) — the shell layer is already partly a thin wrapper around Python logic. This merge extends that existing pattern rather than introducing a new one.

## Target architecture

**One Python core owns all non-terminal-specific logic**, invoked both by the legacy wrapper-driven path and by the new scheduler — no second implementation of routing/DB logic in PowerShell:

- **`db.py`** — factor `channel.py`'s SQL (`_fetch_and_deliver`, `mark_processing`, `mark_processed`, the queued-count query) into plain functions, plus `insert_handoff()`/`verify_queued()` covering what `kiln-handoff/SKILL.md` currently does via prose+SQL. `channel.py` calls into this module instead of inlining SQL twice — behavior-preserving.
- **`routing.py`** — ports `Read-HandoffRoutingTable`'s table parsing to Python, **plus reads a new optional conditional column in `workflow.md`'s routing table** (e.g. `Role | Sends to | When Sender`) to express the specifier/architect override as data. This replaces the originally-proposed Python-only `ROLE_ROUTING_OVERRIDES` constant — the routing spec stays in one human-editable place (`workflow.md`) instead of being split across markdown prose and a Python constant. Both the scheduler and the legacy wrapper path read this same module/table.
- **`worker_prompt.py`** — builds the backend-agnostic one-shot prompt content from the handoff + worker-agent file.
- **`status_contract.py`** — the `KILN-STATUS` sentinel format and `parse_worker_report(stdout) -> WorkerResult(status, summary)`; owns the canonical instruction text so the parser and the generated instructions can't drift apart.
- **`adapters/{claude,copilot,codex}_adapter.py`** — one `run_worker(role, agent_def_path, prompt, cwd, model=None) -> str` per backend (see required spike below). Each `run_worker` enforces a wall-clock timeout (configurable per role, default ~15 min); a timeout is treated identically to a `KILN-STATUS: blocked` result and flows through the same retry-once-then-escalate policy below — no separate code path, since a hang (e.g. a stalled approval prompt) is exactly the kind of thing a second attempt might not repeat.
- **`role_scheduler.py`** — the main loop, wiring the above together (see loop below).
- **`kiln.ps1`/`kiln.sh` shrink to per-platform terminal-launch shims**: generate the WezTerm Lua config / `wt.exe` args / tmux `send-keys` commands, and shell out to the shared Python modules (via a small Python CLI entrypoint) for everything else — project init/scaffolding, profile/config loading, routing, DB ops. This is the concrete resolution of #5: not a big-bang full port on day one, but the shell layer never again grows a second copy of logic that already exists in Python. Where/when the remaining shim logic itself eventually becomes Python (`kiln.py`) instead of PowerShell/zsh is a sequencing detail, not a separate architectural question — the "one Python core" decision is what matters.

Scheduler main loop (mirrors today's wrapper loop 1:1, as code instead of prose):

1. Poll `messages` for `target=<role> AND branch=<branch> AND status IN ('queued','delivered')`, ordered by `priority, created_at` — same query as `channel.py`'s `_fetch_and_deliver`.
2. Mark `delivered`, then `processing`.
3. Write `tmp/handoff-in.md` verbatim (parity with `/kiln-receive`, for human debuggability).
4. Extract `Sender:`, `Handoff:` (the specifier's stable handoff name — must be carried forward unchanged into the next handoff), `Branch:`, `Commit:` via regex; `git merge <commit>`. On merge failure: stop, do not proceed to delegation, write an error/needs-human message instead.
5. Detect `Kiln-Ping: true` and branch to the ping-trail-append path instead of normal delegation.
6. Build the worker prompt: the pre-generated worker agent content (`.claude/agents/<role>-worker.md` / `.github/agents/<role>-worker.agent.md` / `.codex/agents/<role>-worker.toml`, already produced by `Write-GeneratedWorkerAgent` — reused as-is) + the full `tmp/handoff-in.md` content + branch/worktree info + the `KILN-STATUS` instruction.
7. Invoke the per-backend one-shot adapter, capture stdout, parse the sentinel.
8. If `blocked`: retry once, folding the failure report into a new prompt. If blocked again: escalate (see below) instead of a normal handoff.
9. On success: squash commits since the merge anchor, format the handoff message from the same template in `workflow.md` (carrying forward `Sender:`/`Handoff:`/new `Branch:`/new `Commit:`), `INSERT`, `SELECT`-verify with retry-on-failure.
10. `mark_processed`, call `set-status.py <role> <state>` at each transition, loop to 1.

## Testability by design

`kiln/project/constitution/engineering.md` is scaffolding Kiln writes into *projects it orchestrates* (its header: "Copied into `<project>/...` during project init") — it is not a constitution that currently governs Kiln's own framework source, and nothing today holds `kiln/framework/`'s existing Python (`mcp-server/*.py`, `tools/*.py`, both currently untested) to it. This plan borrows its principle anyway as a deliberate choice, not an existing obligation: *"Separate testable modules from environmentally unsuitable modules... Maximize testable code and minimize the unsuitable boundary."* It's a well-stated idea worth applying to the framework's own code, and it would be inconsistent to hold downstream projects to a bar the tool orchestrating them doesn't meet itself. Every module in this plan is split along that line from the start, not refactored for testability later:

- **`role_scheduler.py`** exposes `run_once(ctx: SchedulerContext) -> CycleResult` as the real unit of behavior — one poll/merge/delegate/handoff cycle, no loop, no `sleep`, no `while True`. `main()` is a thin wrapper: `while True: run_once(ctx); sleep(poll_interval)`. `SchedulerContext` bundles injected dependencies (`db_path`, `run_worker: Callable`, `clock: Callable[[], datetime]`, `git_runner: Callable`) so `run_once` can be called directly, repeatedly, deterministically, from a test with no real time elapsed and no real subprocess spawned.
- **`adapters/*.py`** split `build_command(role, agent_def_path, prompt, cwd, model=None) -> list[str]` (pure, unit-testable — no process spawned) from `run_worker(...)` (thin: calls `subprocess.run(build_command(...), timeout=...)`, parses nothing itself, is the one line per adapter that's "environmentally unsuitable" and gets integration- not unit-tested).
- **`db.py`** takes `db_path`/`conn` as an explicit parameter on every function — no module-level global connection — so tests point it at a `tmp_path` SQLite file with the real schema, not a mock.
- **`routing.py`** and **`status_contract.py`** are pure functions over strings/dicts in, dataclasses out — no I/O at all, fully unit-testable with zero fixtures beyond in-memory strings.
- **`worker_prompt.py`** is pure string assembly — same as above.

This split is also what makes the mutation-testing gate meaningful: `mutmut` against a module that's 90% subprocess/IO plumbing produces mostly-unkillable mutants and a misleading score. Concentrating the branchy/decision logic (routing precedence, sentinel parsing, retry/escalation counting, cycle state transitions) into the pure modules above is what makes a real mutation score possible.

## Test strategy

Every Python artifact introduced or touched by this plan gets unit tests as a hard requirement — a merge gate for each PR in the sequencing above, not aspirational. This mirrors the TDD/mutation-testing discipline `engineering.md` and the `mutation-testing`/`run-mutation` skills already impose on projects Kiln orchestrates (same tools — `mutmut`, `radon` — same rough thresholds), applied here to the framework's own code as a new, self-imposed bar rather than an inherited one. Three tiers, run at different points:

### 1. Unit (fast, no I/O, run on every commit)

| Module | What's tested |
| --- | --- |
| `db.py` | fetch/mark/insert/verify against a real scratch SQLite file (`tmp_path`, real schema — not mocked, SQLite is fast enough that "unit" and "real file" aren't in tension); ordering by `priority, created_at`; empty-queue behavior; malformed-row tolerance |
| `routing.py` | base table parsing (parity fixtures ported from today's `Read-HandoffRoutingTable` behavior); the new conditional column — precedence when multiple rows match a role, default/fallback row when "When Sender" is blank, missing-table and malformed-table error paths |
| `status_contract.py` | golden `KILN-STATUS: done`/`blocked` outputs; missing sentinel; sentinel present but malformed (no summary line, extra whitespace, wrong casing); sentinel buried mid-output vs. required-last-line — decide and lock the exact matching rule here, since this is the single piece standing in for LLM judgment |
| `worker_prompt.py` | correct field substitution and section ordering for each of the three worker-agent-file formats (`.md`, `.agent.md`, `.toml`) |
| `adapters/*_adapter.py` (`build_command` only) | correct argv construction per backend, including the "no MCP config" isolation flags once the spike confirms them |
| `role_scheduler.py` (`run_once`, retry/escalation counter, circuit breaker) | as pure functions over injected fakes — see integration tier for the wiring, this tier covers branch logic: blocked-once-retries, blocked-twice-escalates-and-suppresses-handoff, 3-consecutive-escalations-halts-and-resets-on-success |

Target: mutation score ≥ 80% (`mutmut`, per-file per the `run-mutation` skill's sequential protocol) on `routing.py`, `status_contract.py`, and `role_scheduler.py`'s decision logic specifically — higher than the ≥70% the `mutation-testing` skill documents as typical for downstream projects, because this is coordination-critical code where a silently-wrong routing or escalation decision fails a whole swarm cycle rather than one feature. `db.py`/adapters' `build_command` at that ≥70% baseline. Run `radon` for complexity/duplication the same way that skill already does for downstream code.

### 2. Integration (real SQLite + real git, fake agent CLI, run on every PR)

The key technique for testing the *whole mechanical loop* without live LLM cost or nondeterminism: a **fake agent binary** (`tests/fixtures/fake_agent.py`, one tiny script that reads its prompt and writes a canned `KILN-STATUS:` line to stdout, configurable via env var to return `done`, `blocked`, hang-past-timeout, or emit a truncated/malformed sentinel) substituted for `claude`/`copilot`/`codex` via the injected `run_worker` in `SchedulerContext`.

With that fake in place: `run_once` is exercised end-to-end against a real `tmp_path` git repo (real branches, real commits, real merges — git is already a hard project dependency, no reason to mock it) and a real scratch `messages.db`, covering:

- happy path: queued message → merged → worker invoked once → sentinel parsed → squash commit with correct `[Role] ...` prefix → handoff row inserted for the correct target → original message reaches `processed`
- merge conflict → stop-and-report path, no delegation attempted
- `Kiln-Ping: true` → ping-trail-append path instead of normal delegation
- blocked-once → retry → done: exactly two worker invocations, one handoff
- blocked-twice → escalation message with `Kiln-Escalation: true`, no normal handoff, inbound message still reaches `processed`
- worker timeout (fake agent sleeps past the timeout) → treated identically to blocked, per the adapter contract above
- 3 consecutive escalations → role halts, loud log line, one message to `human-in-the-loop`; a later successful cycle resets the counter (needs a path to "unhalt" for the test — decide whether halt is process-exit or an internal flag before writing this test, since it changes what "resets on success" even means operationally)

This tier also covers `channel.py`'s refactor: run its existing MCP tool entry points (`wait_for_message`, `mark_processing`, `mark_processed`) against the same real scratch DB before and after the `db.py` extraction and diff the behavior — this is the regression check that "behavior-preserving" actually held.

### 3. Acceptance / live (real vendor CLIs, gated — manual or a separate opt-in CI job, not run on every PR)

This is the tier the doc's existing "Live one-cycle smoke test" / "Live multi-cycle swarm run" / spike items already describe — kept as-is, but now explicitly the *only* tier touching real `claude`/`copilot`/`codex` processes. Everything that can be pushed down into tier 1 or 2 should be, specifically so this tier — the expensive, slow, credential-dependent, non-deterministic one — stays as small as possible: proving the vendor CLI integration works, not re-proving the scheduler's own logic.

### Non-Python shim testing (explicitly separate concern)

`kiln.ps1`/`kiln.sh`'s new scheduler-launch branches are shell/PowerShell, not Python, so they fall outside "every Python artifact" — but they shouldn't fall outside testing entirely. Out of scope for this plan to design in full, but flag: they need their own smoke coverage (Pester for `kiln.ps1`, `bats` or equivalent for `kiln.sh`) verifying the correct command string is emitted for a scheduler-opted role, at minimum — otherwise the one integration point between the well-tested Python core and the real terminal launch is exactly the piece with zero automated coverage.

## Changes to existing files

- **`bin/kiln.ps1`**
  - `Get-WindowsTerminalAgentCommand` / `Build-WezTermAgentCommand`: new branch, gated by a per-role scheduler flag, emitting `python role_scheduler.py --role ... --branch ... --db-path ... --agent ... --worktree ...` instead of `claude`/`codex`/`copilot`, only when `Mode -eq "auto"` and the role opts in.
  - `Write-GeneratedWorkerAgent`: append a shared status-contract snippet (single source of truth with `status_contract.py`) to every generated worker file so its last output line is always `KILN-STATUS: done|blocked` + summary.
  - `Write-GeneratedCLAUDEmd`: skip entirely for scheduler-driven roles (no wrapper LLM session to read `CLAUDE.md`/`workflow.md`); unchanged for manual-mode and non-opted-in roles.
  - `Read-HandoffRoutingTable`: retired in favor of calling `routing.py` once all wrapper-driven roles migrate; until then, both must agree on the same `workflow.md` table format (including the new conditional column) to avoid drift.
- **`bin/kiln.sh`**: mirror the same changes at `write_agent_instruction_file`, `write_worker_agent_file`/`write_codex_worker_agent_file`, and `launch_role`'s command-building `case`. Unix's pane-creation shim (tmux `send-keys`) only needs to send the same Python command line — the OS-agnostic core logic requires no separate Unix port, narrowing the pre-existing "Unix parity" gap to this shim.
- **`kiln/project/constitution/workflow.md`**: add the optional conditional routing column described above.
- **`kiln/framework/mcp-server/channel.py`**: refactor to call `db.py` instead of inlining SQL — behavior-preserving.
- **`kiln/framework/profiles.json`**: add an opt-in `"scheduler": "python"` flag alongside each `terminals[]` entry (default absent = today's LLM wrapper), read into a new `$global:SCHEDULERS` array parallel to the existing `$global:MODES`.
- No changes to `kiln/project/skills/kiln-handoff/SKILL.md`, `kiln-receive/SKILL.md`, or `kiln/project/roles/*.md` — they remain the canonical prose spec for legacy-wrapper roles and the reference to diff `role_scheduler.py`'s behavior against.

## Resolved decisions

- **Conditional routing precedence** — a rule whose `When Sender` matches the inbound sender wins over the rule with a blank `When Sender`, which is the role's default. Precedence is by specificity, not row order, so reordering the table cannot change routing. A role with only conditional rules and no default resolves to nothing for unlisted senders rather than guessing.
- **Duplicate rules are a hard error** — two rows competing for the same `(role, When Sender)` pair raise at parse time instead of silently picking one, since a misrouted cycle is far more expensive to debug than a startup failure.
- **Sentinel matching** — the parser scans lines from the END backwards and takes the last `KILN-STATUS:` line, matching the prefix and status word case-insensitively with an optional space after the colon. Leniency is deliberate: misreading a *successful* run as blocked because of casing or a trailing banner line is the costlier failure. A missing, truncated, or unrecognised sentinel all resolve to `blocked`, but `WorkerResult.sentinel_found` distinguishes "reported nonsense" from "never reported" so escalation messages can say which.

## Status: PR #1 delivered

PR #1 (Python core extraction, behaviour-preserving) is implemented and verified:

| Artifact | Detail |
| --- | --- |
| `kiln/framework/scheduler/{db,routing,status_contract}.py` | 212 statements, **100% line coverage** |
| `tests/{test_db,test_routing,test_status_contract,test_channel_server}.py` | **117 tests**, all passing |
| Mutation score | **391 mutants, 0 survivors (100%)** — exceeds both the ≥80% and ≥70% tiers |
| Lint / complexity | `ruff` clean; radon average A (4.08), no block worse than B |
| `kiln/framework/mcp-server/channel.py` | refactored onto `db.py`; regression-tested via a stubbed MCP transport |

Tooling notes for whoever picks this up:

- **`mutmut` cannot run on native Windows** (it requires WSL, and the only installed distro here is `docker-desktop`). Mutation testing uses **`cosmic-ray`** instead — Windows-native, configured in `tests/mutation/*.toml`. `constitution/engineering.md`'s tool table still names mutmut for *downstream projects*, which is unaffected; this is Kiln's own toolchain choice.
- Dev tooling lives in a project-local `.venv/` per `engineering.md`'s "prefer project-local paths"; config is in the new root `pyproject.toml`.

## Status: launcher fully ported to Python

`bin/kiln.ps1` (1826 lines) and `bin/kiln.sh` (1377 lines) are now ~40-line shims that set `PYTHONPATH` and forward to `python -m launcher.cli`. All logic lives in `kiln/framework/launcher/`.

| Module | Replaces |
| --- | --- |
| `paths.py` | ~20 inline `Join-Path` chains |
| `config.py` | `lib/profile-loader.ps1` + `Load-ConfigFromProfile` |
| `commands.py` | `Build-WezTermAgentCommand` + `Get-WindowsTerminalAgentCommand` + `kiln.sh`'s tmux copy |
| `templates.py` / `generate.py` | `Get-Kiln*`, `Write-GeneratedCLAUDEmd`, `Write-GeneratedWorkerAgent`, `Write-ClaudeConfig` |
| `workspace.py` | `Initialize-GitRepo`, `Install-GitHooks`, `Prepare-*` (8 functions) |
| `terminals/{wezterm,windows_terminal,tmux}.py` | `lib/terminal-adapters/*` + `kiln.sh`'s tmux path |
| `scaffold.py` | `Invoke-KilnInit` + 9 `*-KilnInit*` helpers |
| `stop.py` / `cli.py` | `-Stop` (WMI) + the main flow |

**468 tests passing, ruff clean.** Verified end-to-end on a real project: `kiln init --example library-hub` → scaffold → launch, both directly and through the PowerShell shim, with the `scheduler-coder` profile correctly switching the coder pane to `python -m scheduler.role_scheduler`.

Design notes:

- **Commands are built once as structured data** (`AgentCommand`: argv + env + banner) and rendered per host shell (`render_powershell` / `render_posix`). The three copies that had to agree on both flags and quoting are now one.
- **The scheduler is launched as `python -m scheduler.role_scheduler`, not a bare script path** — the package uses relative imports, so a script path fails with "attempted relative import with no known parent package". `PYTHONPATH` points at `kiln/framework`.
- **The launcher and scheduler import only the standard library**, so they run on bare system `python` with no install step. (`channel.py` still needs `mcp`.)
- **Unix parity is now structural** rather than maintained: tmux is one more `terminals/` module over the same core, which closes the substance of the "kiln.sh parity" issue.

### Not yet done

- **A real WezTerm GUI launch.** The generated Lua is confirmed to *parse and load* (`wezterm --config-file … show-keys` succeeds), but `gui-startup` — the pane/split creation — only runs on an actual GUI start and has not been exercised.
- **Dead shell code is left in place**, not deleted: `lib/profile-loader.{ps1,sh}`, `lib/terminal-adapter.sh`, `lib/terminal-adapters/*`. `lib/kiln-window-watchdog.sh` is separate functionality that was never ported, and README still references these paths — worth a deliberate cleanup pass rather than folding deletions into this diff.
- **Pester/bats coverage of the shims** is no longer needed for logic (there is none left), but the two shims themselves are untested.

## Bugs found while porting the launcher

1. **The live status bar has never worked.** The WezTerm Lua reads `Kiln_PROJECT_DIR` (`wezterm.ps1:123`), but no PowerShell code ever set it — only `Kiln_ROLES_JSON` and `Kiln_LAYOUT_JSON`. With it empty, the `update-status` handler returns on its first line, so the per-role status badges shown in the README screenshot never render. The Python launcher exports it.
2. **A stale `CLAUDE.md` leaks into scheduler workers.** Skipping the write for scheduler roles is not enough — a role switched over from the wrapper keeps the previous run's file, and the Claude spike proved a stray `CLAUDE.md` *is* read by one-shot workers. `write_instructions` now deletes it.
3. **`.Kiln` vs `.kiln` casing** in the Lua's pane-ids path — harmless on case-insensitive Windows, would create a stray directory on Unix.

Also worth knowing, not a bug: **symlink creation fails on this machine** (`WinError 1314` — needs Developer Mode or an elevated shell), so worktrees fall back to *copying* `.kiln` rather than sharing it. The fallback keeps the swarm working, but shared state is not actually shared; enabling Developer Mode would fix it.

## Status: PR #2 Python layer delivered

The scheduler and Claude adapter are implemented and verified. What remains of PR #2 is the shell-side wiring (`kiln.ps1`/`kiln.sh` launch branch, `profiles.json` opt-in flag) — deliberately left as its own slice since it is PowerShell/bash and outside the pytest gate.

| Artifact | Purpose |
| --- | --- |
| `scheduler/handoff.py` | Parse/format workflow.md's message format, incl. ping trails and the escalation field |
| `scheduler/worker_prompt.py` | Reads the *existing* generated worker agent file; builds the `--agents` payload and task prompt |
| `scheduler/git_ops.py` | Merge, squash-to-anchor, local-exclude management |
| `scheduler/adapters/claude_adapter.py` | One-shot invocation; every spike finding encoded as a pinned test |
| `scheduler/role_scheduler.py` | `run_once(ctx, state)` — one full cycle, no loop, no sleep; `main()` is the only loop |

| Check | Result |
| --- | --- |
| Tests | **279 passing** (was 117 after PR #1) |
| Line coverage | **100%** across all 10 scheduler modules (683 statements) |
| Mutation (pure modules) | **385 mutants, 0 survivors** — routing, status_contract, handoff, worker_prompt |
| Lint / complexity | `ruff` clean; radon average A (2.96), nothing worse than B |

Deferred: mutation testing for `role_scheduler.py`/`git_ops.py`/`claude_adapter.py`. Their tests drive real git and SQLite (~25s per suite run), so a full mutation pass costs hours rather than minutes. Needs either parallel distribution or a scoped subset before it is practical.

### Design points worth knowing

- `run_once(ctx, state)` takes every outside-world dependency (worker invocation, clock, status writer) through `SchedulerContext`, so the whole cycle is driven in tests by a **fake worker** — every branch (retry, escalation, circuit breaker, merge conflict, ping) is exercised with zero LLM cost and no flakiness.
- `should_retry(...)` is a pure function over a sequence of attempts, testable with no DB, git or worker at all.
- A worker that **changes nothing** still hands off successfully (an architect can validate and find nothing to change) — it simply reuses the merge commit as its handoff commit.
- Escalation, circuit-breaker and no-route paths all still `mark_processed` the inbound message, so nothing can wedge in `processing`.

## Bugs found while implementing PR #2

Both were caught by tests before ever running live, and **both also affect the legacy prose path**, not just the scheduler:

1. **Fast-forward merges destroy history.** `git merge <commit>` fast-forwards when the receiving branch has not diverged, producing **no merge commit**. `squash_anchor` then finds none and falls back to the repository's **root commit** — and the next `git reset --soft <root>` collapses the entire project history into a single commit. Fixed by forcing `--no-ff` so every cycle has a well-defined anchor. Note that `kiln-handoff/SKILL.md` step 2 specifies the same root-commit fallback, so a wrapper-driven role is exposed to this too whenever a cycle happens to fast-forward.
2. **The scheduler deadlocked itself on its own debug file.** `tmp/handoff-in.md` is written every cycle for parity with `/kiln-receive`. Because `squash_since` stages with `git add -A`, that file was committed, and the *next* cycle's merge then aborted with "untracked working tree files would be overwritten by merge". Fixed via `git_ops.ensure_ignored("tmp/")`, which writes to `.git/info/exclude` (local-only, never modifies the user's `.gitignore`, and resolves correctly inside linked worktrees). Kiln's own project template happens to ignore `tmp/`, which is why this has not bitten the wrapper path — but nothing enforced it.

## Status: first live end-to-end cycle (2026-08-08)

`scheduler-all` on a clean `library-hub-testrun3`. A human handoff reached the specifier, which merged, ran a one-shot worker (14 tool calls, $0.61, `KILN-STATUS: done`), squashed and queued a handoff to the coder — **the full cycle worked on its first live run.** The coder then failed to merge it, which exposed one real bug and two presentation problems.

### Bug: the squash sweeps up Kiln's own scaffolding (same class as the `tmp/` deadlock)

`.claude/settings.json` is copied, untracked, into every worktree by the launcher, and was **not** in `REQUIRED_GITIGNORE_ENTRIES`. `squash_since`'s `git add -A` committed the specifier's copy; the coder's next merge aborted with "untracked working tree files would be overwritten" and escalated to `human-in-the-loop`. One handoff in, the swarm stopped.

This is bug #2 from PR #2 recurring with a different file, which means the per-file fix was the wrong shape. Now addressed at three levels:

1. **Prevention** — `git_ops.GENERATED_WORKTREE_PATHS` names the whole launcher footprint (`tmp/`, `.claude/settings.json`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md`), and `ensure_generated_ignored` force-ignores all of it at scheduler startup and on every inbound handoff.
2. **Contract** — `.claude/settings.json` added to `launcher.workspace.REQUIRED_GITIGNORE_ENTRIES`, so newly scaffolded projects ignore it in their own committed `.gitignore`.
3. **Recovery** — `merge_commit` now parses the "untracked working tree files" error and retries once after deleting the blockers, but **only when every listed path is Kiln-generated**. One unrecognised path and the merge fails loudly instead, because deleting a worker's real output would be far worse than a stalled cycle. This matters because prevention cannot help a repo whose history already carries the file.

Verified against the actual poisoned commit from the live run, in a throwaway clone: the old code reproduces the abort exactly, the new code recovers and merges.

### Presentation fixes

- **The pane opened on the echoed command line.** WezTerm's `send_text` and tmux's `send-keys` type into a live prompt, which echoes; `wt.exe` passes `-Command` and does not. The renderers take a `clear` flag, set only for the two echoing backends, and the scheduler now prints a **config banner** — role, branch, resolved worker/model, resolved routing, worktree, workflow, queue, timeouts, log path. Routing is on it deliberately: it is the most surprising piece of config, and this makes a misroute diagnosable before it happens.
- **Tool calls showed only a name.** A pane of bare `🛠 Bash` lines says nothing. `summarise_tool_use` adds the one input field that matters per tool (command, file path, pattern, url…), collapsed to one line and truncated at 140 chars, with a fallback for tools the CLI adds later. Failing tool results now also get a line — a failed tool is usually *why* a worker ends up blocked, and it was previously invisible.

Suite after this round: **545 passing**, ruff clean.

### Per-pane status bar (`scheduler/pane_status.py`)

A colored status line pinned to the **bottom** row of each scheduler pane, showing role, state, cycle count, accumulated cost, handoff target and the last summary. Implemented with DECSTBM (the VT scrolling region), so the pane stays an ordinary terminal — selection, copy/paste and scrollback all keep working, and only the last row is reserved.

Bottom rather than top is a technical constraint, not a preference: **a terminal only pushes scrolled-off lines into scrollback when the scrolling region starts at row 1.** A top bar needs a region starting at row 2, and terminals then discard those lines instead of retaining them — the pane would scroll but keep no history. The alternative that gets a genuine top bar is a second, dedicated pane per role, at the cost of doubling the pane and process count and only working on WezTerm.

Design points:

- `format_bar` / `style_for` are pure and tested directly; `StatusBar` is the only thing that emits escape sequences. It disables itself when the stream is not a TTY, so piped output and the test suite's own capture stay clean.
- The bar repaints only when the rendered text or the terminal size actually changes — a bar rewriting itself every 2s poll would flicker and fight text selection.
- `attach_status_bar` **wraps** the existing `ctx.set_status`, which already feeds `.kiln/status/<role>.json` for the WezTerm tab-bar badges. Every state change reaches both surfaces from the single existing call site.
- `main()` releases the region in a `finally`, including on a crash. A region that outlives the process leaves the pane's shell prompt behaving strangely long afterwards.
- Also fixed: the WezTerm Lua's `STATE_COLORS` had `handoff`, but the scheduler reports `handing-off`, so that badge always fell through to the default grey. Both spellings are now listed, since manual roles still run the wrapper.

100% line coverage on the module. Suite: **585 passing**.

### Bug: the shipped cycle could never terminate

The constitution's routing table routed **every** specifier handoff to `coder`. The one exception — an architect's completed-cycle report goes back to the human rather than around again — lived only as a prose note *underneath* the table, pointing at `roles/specifier.md`.

That was survivable while a wrapper LLM did the routing, because it could read the note. The scheduler reads the table. So a finished cycle fed straight back in:

```text
architect -> specifier -> coder -> refactorer -> architect -> ...
```

forever, and the human was never told the work was done. This is precisely the gap decision #3 of this plan called out — `routing.py` shipped with three-column support in PR #1, but no constitution was ever migrated to use it, so the capability sat unused.

The shipped table now carries the condition as data:

```text
| Role      | Sends to          | When Sender |
| specifier | coder             |             |
| specifier | human-in-the-loop | architect   |
```

`roles/specifier.md` still governs *what* the specifier does with such a message (forward as-is, do not re-run the Gherkin workflow); only the routing decision moved into the table.

Guarded by `TestShippedRoutingTable`, which walks the graph the way the scheduler does and asserts it returns to `human-in-the-loop` within a bounded number of hops. Verified to fail against the old two-column table.

Suite: **589 passing**.

Unrelated leftover spotted while grepping and now removed: `temp/claude/*.md` and `temp/copilot/*.md` were auto-generated wrapper instruction files from 2026-06-24, committed by accident and referenced by nothing. They still carried the old two-column routing table, so anyone grepping for it found four stale copies alongside the real one. Their generator no longer exists — `launcher/generate.py:write_instructions` writes into each worktree instead. Deleted (8 files), along with the now-dead `"temp"` entry in `pyproject.toml`'s ruff `extend-exclude`.

## Documentation & dead-code audit (2026-08-08)

A sweep for unused files and doc/reality drift after the port settled. It turned up **two silent port regressions** that no test covered, which is the part worth remembering — the docs were right and the code was wrong.

### Port regressions found by reading the docs against the code

1. **`-ProfileName` stopped working.** The PowerShell original declared `[Alias("Profile")] [string]$ProfileName` — `-ProfileName` was the *primary* spelling and what the README documented in six places. The Python port kept only `-Profile`, so every existing invocation failed with `unrecognized arguments: -ProfileName`. Restored as an alias, and `TestCliParsing` now pins every spelling of every flag in the original param block, not just this one.
2. **`/etc/kiln/profiles.json` stopped being searched.** The two shell originals disagreed: `profile-loader.ps1` looked in `C:\ProgramData\kiln\`, `profile-loader.sh` in `/etc/kiln/`. The port merged the cascade but kept only the Windows path, so the documented system-wide location silently died on Unix. Now `SYSTEM_PROFILES_PATH` picks per platform, with `TestSearchPaths` asserting the whole documented cascade in order.

Both are the same failure mode: the port was validated against *behaviour on this machine*, and neither the dropped alias nor the Unix-only path is reachable from a Windows test run.

### Dead files removed

- `lib/` entirely (10 files): `profile-loader.{ps1,sh}`, `terminal-adapter.sh`, `terminal-adapters/{wezterm.ps1,wezterm.sh,windows-terminal.sh,terminal-app.sh,ghostty.sh,none.sh}`, `kiln-window-watchdog.sh`. Superseded by `launcher/config.py` and `launcher/terminals/*.py`.
- `bin/kiln-cleanup.sh` — **had been broken since before the port**: it sourced `"$SCRIPT_DIR/terminal-adapter.sh"` with `SCRIPT_DIR=bin/`, but the file lives in `lib/`, so it aborted on line 21 under `set -euo pipefail`. Its usage string still said `swarm-cleanup.sh` and took a tmux socket, while the README documented `kiln-cleanup.sh <path-to-project>`. Removed rather than left looking like a feature; the Unix cleanup gap is now stated explicitly in the README.
- A stale `.claude/settings.json` permission entry for `lib/profile-loader.ps1`, and the now-dead `"temp"` ruff exclusion.

`lib/kiln-window-watchdog.sh` was the one deletion with real lost functionality — reopening closed terminal surfaces, never ported. The README's tmux section had described it as current behaviour.

### Documentation corrected

The README had drifted badly enough that its architecture section described a system that no longer existed:

- **The scheduler was entirely undocumented** — zero mentions of `scheduler`, `launcher/`, or `python -m`, despite being the largest change. Added an **Execution Modes: Wrapper vs Scheduler** section with the per-role opt-in, the sentinel contract, the trade-offs, and a comparison table.
- **Platform Support** claimed Unix needed zsh and PyYAML. Neither is true — nothing has imported yaml since profiles became JSON, and the shim is `#!/usr/bin/env bash`.
- **Framework Structure** listed the deleted `lib/` tree and omitted `launcher/` and `scheduler/` completely.
- **Adding A Terminal Backend** documented a shell function contract (`terminal_open_session`, `terminal_window_exists`, …) that no longer exists; replaced with the Python `launch(panes, layout, dry_run)` interface.
- **tmux Behavior** claimed a project-specific socket, `base-index` handling and a window watchdog. `terminals/tmux.py` does none of these.
- **Cleanup** documented the broken script; now split into `--stop` (safe) vs full reset (destructive, Windows only).
- **Profile Loading & "Inheritance"** — there is no inheritance. `find_profiles_config` returns the first file that exists; a project's `kiln.profiles.json` *replaces* the framework set rather than extending it, so a one-profile file silently removes `default`, `compact` and `scheduler-all`. Now called out with a warning. Two locations documented as "Not used" are in fact searched.
- **Project Maturity** was pinned at v0.2/Phase 6; added Phase 7 and rewrote Known Limitations (Unix parity is no longer a gap; scheduler mode is Claude-only; no Unix full reset).

Suite: **604 passing**.

## Bug: kiln-channel silently dead against mcp 2.0 (2026-08-08)

Symptom: a wrapper-mode role ran one cycle, then started asking its human for confirmation on everything instead of receiving handoffs. The role's own diagnosis was right — `channel.py` imports `mcp.server.fastmcp.FastMCP`, and the installed SDK was **mcp 2.0.0**, which deleted that module. Verified directly: `mcp.server` in 2.0 exposes `mcpserver`, `lowlevel`, `session`, … and no `fastmcp`.

**The fix is not a version pin.** `.mcp.json` names the bare command `python`, so the server runs under the *user's* interpreter — one Kiln neither controls nor installs into. Pinning `mcp<2.0` in `requirements.txt` would only move the failure to whoever last ran pip, and downgrading a global site-packages install to satisfy Kiln is not Kiln's call to make.

Instead `channel.py` supports both releases. The surface it uses — `@server.tool()` and `.run()` defaulting to stdio — is identical in `FastMCP` and 2.0's `MCPServer`, confirmed by inspecting both signatures and registering a tool against the new class:

```python
try:
    from mcp.server.fastmcp import FastMCP          # mcp 1.x
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP   # mcp 2.x
```

Verified end to end with a real JSON-RPC stdio handshake against the server under mcp 2.0.0: `initialize` succeeds and `tools/list` returns all four tools (`wait_for_message`, `get_channel_status`, `mark_processing`, `mark_processed`).

### The worse problem: the failure was invisible

An MCP server that fails to start produces no Kiln-visible error at all. The role just cannot receive, so it falls back to asking its human — which reads as a confused agent rather than a missing dependency. This is the second time the same class of failure has bitten (the first was no `mcp` installed at all).

`cli.warn_if_channel_unavailable()` now runs at launch, probing the **exact command `.mcp.json` will use** with the same import `channel.py` performs, and warns with the affected role names and a `pip install -r` remedy. Deliberately a warning, not a hard failure: a fully scheduled swarm needs no MCP at all, and the check skips entirely when no role is in wrapper mode.

`TestChannelPreflight` covers it, including a test that reads `channel.py` and asserts the probe still matches its imports — otherwise the probe could start passing while the server still fails.

Note this is another argument for scheduler mode: it talks to SQLite directly and has no MCP dependency to break.

Suite: **610 passing**.

## Human entry point: inbox pane + send CLI (2026-08-09)

The `human-in-the-loop` role was asked to be two mutually exclusive things in one LLM session. As an entry point it must sit idle at a prompt the human can type into; as a swarm participant it must be blocked inside `wait_for_message()`, which polls `while True:` with **no timeout**. `loop-manual-claude.md` step 5 forces the second ("Immediately return to Step 1 ... without waiting for the user"), so after one completed cycle the session enters an unbounded blocking tool call and stops being reachable. There is no state where it both listens and talks.

Evidence: `coder -> human-in-the-loop` sat `queued` with `delivered_at = NULL` for a day. Nobody was ever told the swarm had stopped. Escalations and cycle completions both terminate at the human, so this is the role where a lost message costs most — and it was the only one with no retry path.

Two Python modules split the role's two jobs, mirroring what `role_scheduler` did for the agent roles:

- **`scheduler/inbox.py`** — watches a role's queue, performs the deterministic half of `/kiln-receive`, prints each message with framing (sender, time, escalation/ping/handoff), rings the terminal bell, marks it processed. Displayed *is* processed for a human: their reply is a new message, and leaving it `delivered` makes `fetch_and_deliver` re-serve it every poll forever.

  **The first version only notified, which was not enough.** `human-in-the-loop` is a real role in the graph — it works in the project root on the base branch — so an inbound handoff must be *merged into its tree* or the person is reading a description of work that is not there. The inbox now does `/kiln-receive` step 1 (persist `tmp/handoff-in.md`, which is also how a person's own Claude session picks the message up — there is no way to inject one into a running session) and step 4 (`git merge <commit>`), reusing `git_ops.merge_commit` and the now-public `role_scheduler.persist_inbound`. A failed merge is shouted about in the pane and turns the status bar red, but the message is still marked processed: left queued it would be re-served on every poll by nobody. `--no-merge` and an absent `--worktree` both reduce it to display-only, so an ad-hoc `kiln inbox` never touches a repo it was not given.
- **`scheduler/send.py`** — inserts a handoff directly. No MCP, no LLM, so a human can start or unblock a cycle even when the agents are the broken thing. A new request carries no commit, so `is_mergeable` is false and the receiver takes the same path as a ping.

Both are reachable as `kiln inbox` / `kiln send`, intercepted in `cli.main()` before the main parser (adding argparse subparsers would change how the PowerShell flag spellings are matched). `resolve_queue_context()` fills in `--db-path` and `--branch` from the project — branch especially, since messages are branch-scoped and an inbox on the wrong branch is indistinguishable from an empty one.

### Making it automatic

Per user request, no manual second terminal. `"scheduler": "inbox"` is now a third role kind alongside `"python"`:

```jsonc
{ "role": "inbox", "worktree": "@current", "mode": "manual",
  "scheduler": "inbox", "watches": "human-in-the-loop" }
```

`RoleConfig.is_inbox` gates it out of every per-role step — worktree creation, worker definitions, instruction files, agent configs, MCP config — because it runs no agent. `current_dir_role` also had to exclude it: both it and the human's session live in the project root, so an inbox listed first would have stolen ownership of the root `.mcp.json`.

The WezTerm Lua's simple-split branch gained optional per-pane `direction` and `size` (defaults reproduce the old equal-rightward split), so `scheduler-all` puts the inbox as a 22% strip *beneath* the human's session in the same tab. Validated against the real binary with `wezterm --config-file ... show-keys`.

**Bug in that first attempt** — the inbox came up on the right regardless. The branch test was:

```lua
local grid_cols = tab_def.gridCols or #tab_def.panes
if grid_rows > 1 or grid_cols > 1 then   -- grid branch, direction hardcoded 'Right'
```

`grid_cols` defaulted to the *pane count*, so any tab with two panes took the grid branch — which meant adding the inbox to a previously single-pane tab silently flipped it there, and the per-pane `direction` in the other branch never ran. The condition now tests what the tab declared (`if tab_def.gridRows or tab_def.gridCols`), which is equivalent for every existing profile: a 1×N grid produces exactly the same rightward splits as the simple branch. Pinned by `test_the_grid_branch_is_only_taken_when_a_grid_was_asked_for`.

Verified live against `library-hub-testrun3`: the inbox immediately surfaced the day-old escalation, and `kiln send` queued a request the specifier's scheduler could fetch (test message removed afterwards).

Suite: **650 passing**. The headline test is `TestTheBugThisReplaces`, which drives an escalation from queue to human with no LLM, no MCP server and no agent CLI involved.

Still open: the wrapper `human-in-the-loop` role keeps its blocking receive loop, so nothing forces the new path yet. Once it is trusted, the receive steps can come out of `loop-manual-claude.md` — and at that point `scheduler-all` needs no MCP at all.

## Findings surfaced while implementing PR #1

Both were pre-existing and are **not** caused by this change:

1. **`Read-HandoffRoutingTable` silently drops hyphenated role names.** Its regex matches roles with `\w+`, which does not match `-`, so `| human-in-the-loop | specifier |` never becomes a rule. It goes unnoticed only because the single consumer (`bin/kiln.ps1:849`) falls back to `"specifier"` when a role is missing — which happens to be the correct target for that one role. Any *other* hyphenated role would be misrouted with no error. `routing.py` fixes this and pins it with a regression test. The same regex also scans the entire document, so any unrelated markdown table in `workflow.md` would be read as routing rules; `routing.py` scopes parsing to the `## Handoff Routing` section when present.
2. **`kiln-channel` cannot currently start in this repo.** `.mcp.json` launches `channel.py` with a bare `python`, and the `python` on PATH (3.14.7) has no `mcp` package installed — the import fails before the server starts. Unrelated to the refactor (it would fail identically before), but it means the blocking-receive path is not actually running here today. Worth confirming whether agents resolve a different interpreter, or whether this needs an explicit interpreter/dependency pin.

## Implementation plan: PR sequencing

Implementation ships as staged, independently-reviewable PRs rather than one large change — this mirrors the backend validation order in Rollout below and lets each piece merge (and, if needed, revert) on its own:

1. **PR #1 — Python core extraction, behavior-preserving.** `db.py`, `routing.py`, `status_contract.py`; refactor `channel.py` to call `db.py` instead of inlining SQL. No scheduler code, no new runtime behavior — safe to merge standalone ahead of everything else.
2. **PR #2 — Scheduler + Claude adapter.** `role_scheduler.py`, `adapters/claude_adapter.py`, the `kiln.ps1`/`kiln.sh` scheduler-launch branch gated behind the opt-in flag, `profiles.json` flag support. Enabled for exactly one role/profile per Rollout step 1. Gated on the Claude portion of the spike (see "Required spike" below) passing first.
3. **PR #3a — Copilot adapter.** Its own PR, gated on the Copilot portion of the spike passing.
4. **PR #3b — Codex adapter.** Its own PR, gated on the Codex portion of the spike passing.

A backend that fails its spike (see below) is not a project blocker: that backend simply stays on the legacy wrapper path indefinitely while the others proceed. Nothing about PR #1 or the overall architecture depends on all three backends succeeding.

## Escalation path (blocked twice)

On a second consecutive `blocked` (or a worker crash/timeout), the scheduler inserts a message to `target='human-in-the-loop'` with a new `Kiln-Escalation: true` field (additive, existing parsers unaffected) instead of a normal handoff — suppressing the handoff so blocked work is never silently forwarded as if it succeeded. The scheduler still completes its own bookkeeping (`mark_processed` on the inbound message) so nothing wedges in `processing` forever.

**Circuit breaker (per role):** after 3 consecutive escalations for the same role, the scheduler halts that role's loop only — other roles' scheduler processes are unaffected. The consecutive count resets on any successful cycle. On halt, the scheduler writes a loud log line and inserts one additional message to `human-in-the-loop` stating which role halted and after how many escalations. This exists because, per "Pane lifecycle" above, no supervisor process exists today to notice a wedged headless process on its own — a scheduler that kept looping through the same failure all night would otherwise fail silently.

## Side effects on other open issues (resolved for free by this merge, not separately scoped work)

- **#1 / #2** (Copilot/Codex reliability — worker output invisible, wrapper "gets bored" and asks whether to keep polling): resolved structurally for scheduler-opted roles. The scheduler's own poll loop is real code with no idle-attention failure mode, and worker output is captured directly from the one-shot subprocess's stdout instead of relying on opaque in-session delegation visibility.
- **#4** (Unix / `kiln.sh` parity): narrowed, not eliminated. Since the core logic is OS-agnostic Python, Unix only needs its own thin pane-creation shim (tmux `send-keys` of the same Python command line) rather than a full port of PowerShell's template-injection/mode-handling logic.

## Explicitly out of scope

- swarm-forge's full config-driven N-role topology model (`swarmforge.conf`-style arbitrary pack definitions) — not adopted; the narrower conditional-routing-column fix above covers Kiln's actual gap (a flat routing table plus one conditional case), which is smaller than swarm-forge's general graph config.
- Mixed Claude+Copilot+Codex profile testing and cross-backend handoffs — tracked separately, since that's about backend interop generally, not this migration specifically. To be explicit about the boundary: this means *cross-backend handoffs within a single profile* (e.g. a Claude-scheduled role handing off to a Copilot-scheduled role) — it does not include the sequenced single-backend validation (Claude, then Copilot, then Codex) already described under Rollout below, which is in scope and required.

## Rollout

Opt in per role/profile via the `"scheduler": "python"` flag — no big-bang cutover. Legacy wrapper path (`loop-auto-*.md`) stays fully live for every role not opted in, reading the same `workflow.md` routing table (including the new conditional column) so behavior doesn't fork. Sequenced validation:

1. Claude, single role, single test profile (best-documented one-shot `-p` mode).
2. Copilot, once Claude is stable.
3. Codex last (also closes out the "stops polling" bug for whichever roles migrate).
4. Only widen the default after all three backends are validated on at least one role each — and even then, flip role-by-role/profile-by-profile.

## Spike results: Claude (resolved 2026-08-07, ~$0.80 of live calls)

Run against Claude Code **2.1.224** in a scratch repo seeded with a decoy `CLAUDE.md`, a decoy `.mcp.json`, and a decoy project skill, so isolation claims were *observed* rather than inferred from `--help`.

### Verified adapter invocation

```text
claude -p
  --output-format json                       # structured result envelope
  --model <model>                            # MUST be explicit — see cost finding
  --agents '<json>' --agent <role>-worker    # feeds the worker definition
  --strict-mcp-config                        # verified: zero MCP tools reachable
  --setting-sources project                  # verified: drops user-global plugin skills
  --permission-mode bypassPermissions
  <prompt>
```

with **`stdin` redirected from devnull** and **stdout/stderr captured separately**.

| Spike question | Answer |
| --- | --- |
| One-shot flag | `-p`, confirmed. `--output-format json` returns an envelope with `result`, `is_error`, `total_cost_usd`, `num_turns`, `usage`, `permission_denials`, `terminal_reason` |
| Feed the worker definition | `--agents '<json>'` + `--agent <name>` works — the agent's prompt demonstrably governs the response. Cleaner than `--system-prompt`, and it is the same shape `Write-GeneratedWorkerAgent` already produces |
| Guarantee no `kiln-db`/`kiln-channel` | **Yes** — `--strict-mcp-config` with no `--mcp-config` yields zero MCP tools, verified against a `.mcp.json` that declared one |
| Skills in non-interactive mode | **Yes**, they resolve. But *user-global* plugin skills leak in too; `--setting-sources project` keeps project skills and drops the user's globals |

### Findings that change the design

1. **`--bare` is unusable.** It blocks `CLAUDE.md` as advertised, but its auth is "strictly `ANTHROPIC_API_KEY` … OAuth and keychain are never read" — the live call failed with `Not logged in`. Any subscription/OAuth user cannot use it. Rules out the flag the plan originally leaned toward.
2. **Nothing except `--safe-mode`/`--bare` suppresses `CLAUDE.md`.** Verified with a control: `--system-prompt`, `--agents`/`--agent`, and `--setting-sources project` all still load it (one run explicitly called the decoy out as a prompt-injection attempt). `--safe-mode` does block it *and* keeps OAuth working — **but it also disables all skills**, which workers need.
   **Recommendation:** do **not** use `--safe-mode`. Rely on the plan's existing decision to skip `Write-GeneratedCLAUDEmd` for scheduler roles, so there is no generated `CLAUDE.md` to leak; treat a project's own committed `CLAUDE.md` as legitimate context. Revisit only if leakage causes real trouble.
3. **The default model is Opus.** A trivial one-shot cost **$0.19** on the default vs **$0.02–0.09** on `--model sonnet` — a 5–10× difference on an otherwise identical call. The adapter must always pass `--model` explicitly; never inherit the default. `--max-budget-usd` also exists and is a natural per-invocation safety rail.
4. **`claude -p` blocks ~3s waiting on stdin** when stdin is not redirected, and emits a warning to stderr. `run_worker` must pass `stdin=DEVNULL`, and must capture stdout/stderr **separately** — merging them corrupts the JSON envelope.
5. **Parse the sentinel from the JSON `result` field, not raw stdout.** This refines the plan's `run_worker(...) -> str` contract: the adapter should return the `result` string (plus ideally the cost/error metadata) rather than raw process output.

### End-to-end confirmation

A full worker-shaped invocation was parsed by the real `scheduler/status_contract.py`, yielding `status=done`, `sentinel_found=True`, and a correct summary. Notably the worker's *narrative* also discussed the sentinel format, and the last-line-wins scan rule correctly ignored it and took the real verdict — the leniency rule earning its keep on the first live test.

Still unverified for Claude: worker **timeout** behaviour under a real hang, and whether `is_error: true` reliably distinguishes a crash from a model-level refusal. Both are cheap to cover once `run_worker` exists.

## Required spike before implementation (biggest open risk)

None of the three one-shot invocations are verified — this must be spiked live before committing to the adapter interface. The spike is a standalone, throwaway investigation (scratch scripts, not committed code) done *before* PR #2 is written, not folded into PR #2 itself — the open questions below (e.g. whether Copilot has a one-shot flag at all) could invalidate the adapter interface shape entirely, and that's cheaper to discover outside a PR:

- ~~**Claude**~~ — **RESOLVED, see "Spike results: Claude" below.** PR #2 is unblocked.
- **Copilot**: does a one-shot flag exist at all, output-capture semantics, whether the `.github/agents/<role>-worker.agent.md` tool-allowlist mechanism (works for interactive delegation) applies in one-shot mode too.
- **Codex**: whether `codex exec "<prompt>"` can consume the `.codex/agents/<role>-worker.toml` definition at all, or whether the scheduler must instead read the TOML's `developer_instructions` field and inline it as the literal prompt; how `mcp_servers = {}` isolation translates to `codex exec`'s own config; whether `trust_level = "trusted"` seeding still suffices to avoid approval prompts blocking a headless process with nobody watching.

Also unresolved until spiked: how a worker whose output gets truncated before emitting the sentinel is handled (recommend: treat missing sentinel as `blocked`, not as a scheduler crash).

## Validation

- **Unit, no live LLM**: `db.py`'s fetch/mark/insert/verify against a scratch SQLite DB; `routing.py`'s table parser against fixture `workflow.md` variants including the specifier/architect conditional column; `status_contract.py`'s sentinel parser against good/malformed worker outputs; the retry-once/escalate-on-second-failure policy as a pure function over a sequence of results.
- **Live one-cycle smoke test per backend**: hand-insert one `queued` message, run one scheduler cycle, confirm worker invoked once, sentinel parsed, exactly one squash commit with correct `[Role] ...` prefix, exactly one correctly-formatted row inserted for the right target, original message reaches `processed`.
- **Live multi-cycle swarm run per backend**: confirm no stuck `processing` rows, no duplicate handoffs, byte-for-byte-compatible commit/handoff format vs. the legacy wrapper (a downstream legacy-wrapper role must not be able to tell the difference), and specifically confirm the scheduler doesn't reproduce the Codex/Copilot "stops polling" bug over a long idle wait.
- **Escalation test**: a fixture worker that always emits `KILN-STATUS: blocked` — confirm the `Kiln-Escalation: true` message lands and the inbound message doesn't get stuck in `processing`.
- **Token/cost comparison**: measure old wrapper's steady-state per-cycle tokens (full `CLAUDE.md`/`AGENTS.md` content + every tool-call round trip) against the new steady-state (zero wrapper-LLM tokens; only the worker's own one-shot cost) to substantiate the expected savings with a real number.

### Critical files

- `kiln/framework/mcp-server/channel.py`
- `bin/kiln.ps1` — `Write-GeneratedWorkerAgent`, `Write-GeneratedCLAUDEmd`, `Read-HandoffRoutingTable`, `Get-WindowsTerminalAgentCommand`, `Build-WezTermAgentCommand`, `Load-ConfigFromProfile`
- `bin/kiln.sh` — `write_worker_agent_file`, `write_agent_instruction_file`, `launch_role`, `load_config_from_profile`
- `kiln/project/constitution/workflow.md`
- `kiln/framework/profiles.json`
- `kiln/framework/tools/set-status.py`
