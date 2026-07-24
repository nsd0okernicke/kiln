# Wrapper + Worker-Subagent Architecture for Kiln Role Agents

## Context

Kiln's auto-loop role agents (coder, refactorer, architect, reviewer, selftest — specifier is
manual and out of scope) run a persistent cycle: listen for a handoff → merge → do the role's
work → commit → send a handoff → listen again. Agents periodically stall — they finish work but
never send (or never resume listening for) the next handoff — a ~10% failure rate tracked in
`TODO.md`'s "Handoff Reliability Hardening" section.

Investigation confirmed the likely dominant contributor: steps 1 (listen), 2 (merge), 4 (commit),
5 (send) are **already fully mechanical**, encapsulated in the `/kiln-receive` and `/kiln-handoff`
skills. The *only* step that runs as open-ended, many-tool-call agent work inside the same
long-lived process is step 3 ("apply your role rules" — `kiln/templates/loop-auto-claude.md:13`).
That work (reading files, iterating code, running test suites) is what fills up the wrapper's
context turn over turn and dilutes the comparatively small, static receive/handoff instructions
that are responsible for the steps that go missing.

**The fix:** turn each role's persistent process into a thin wrapper that only ever does
listen → merge → dispatch a disposable subagent for the actual work → commit → send → listen
again. The subagent's full working transcript never enters the wrapper's context — only its final
report does — so the wrapper's per-cycle context stays small and repetitive, keeping the
receive/handoff instructions proportionally dominant every cycle instead of being crowded out.

This was confirmed technically feasible, not just plausible:
- Wrapper agents are launched **interactively**, not headless: `claude --model <M> --permission-mode
  bypassPermissions --mcp-config ./.mcp.json ...` (Windows, `bin/kiln.ps1` ~line 697) /
  `claude --mcp-config ./.mcp.json --append-system-prompt-file <f> --permission-mode acceptEdits
  ...` (Unix, `bin/kiln.sh` ~line 475). No `-p`/`--print`, no `--allowedTools`/`--disallowedTools`
  anywhere — the full Agent/Task tool is already available to every wrapper agent.
- Precedent already exists in this repo: `kiln/skills/review/SKILL.md` dispatches two parallel
  `general-purpose` subagents and aggregates their reports ("Both axes run as parallel sub-agents
  so they don't pollute each other's context").
- `TODO.md` Track C independently names almost this exact design as the long-term reliability
  escalation path ("agent only executes the task and signals completion... agents become smart
  task executors rather than full workflow owners"), corroborating evidence this direction is
  sound, not a novel guess.

## Decisions locked in

1. Apply to **all** auto-loop roles at once (coder, refactorer, architect, reviewer, selftest).
   Specifier stays manual/unchanged.
2. Worker briefing mechanism: **generate a custom per-role subagent definition file**
   (`.claude/agents/<role>-worker.md`) at launch time, mirroring the existing CLAUDE.md
   generation pipeline — not the generic `general-purpose` type with inlined instructions.
3. Failure handling: retry once with the failure report folded into the prompt as feedback; if
   it fails again, escalate.
4. **Both platforms, in this plan.** `bin/kiln.sh` turned out to have no equivalent
   loop-injection mechanic to convert — see "Why Unix needs new plumbing" below. Building it is
   a prerequisite step here, not a separate follow-up.
5. **Escalation routing:** a twice-failed worker's handoff still goes to the *normal*
   Handoff-Routing-table target (`coder→refactorer`, etc.) — only the message content changes to
   report the blocker. No routing-table exception.
6. **Worker status contract:** every worker subagent's final report must end with a literal
   `WORKER_STATUS: DONE` or `WORKER_STATUS: BLOCKED — <reason>` line. The wrapper's
   retry/escalate decision keys off that literal marker, not free-text judgment — consistent
   with this project's existing pattern of explicit protocol markers (e.g.
   `system-communication-test`).
7. **Wrapper's generated CLAUDE.md/prompt gets slimmed further than just the role block.** Once
   work is delegated, the wrapper no longer needs `project.md`/`engineering.md` (language, tools,
   test frameworks, project architecture) at all — that's exclusively the worker's concern now.
   For auto+claude roles, the generated file drops to: role pointer + loop template + runtime
   template + `workflow.md` only. Copilot and manual-mode (specifier) generation is unchanged —
   copilot has no worker-delegation path, so it still needs the full content to do its own work.
8. **Worker files are placed per-worktree** (`<worktree>/.claude/agents/<role>-worker.md`),
   mirroring exactly how `CLAUDE.md` itself is placed — not centralized at the project root. This
   requires a `.gitignore` fix (see below) to avoid repeating a bug this project already hit once
   with `CLAUDE.md` itself.

## Verified technical groundwork

**Windows pipeline** (`bin/kiln.ps1`) — confirmed accurate by direct read:
`Write-GeneratedCLAUDEmd` (kiln.ps1:627-678, called from :1132 and :1156) assembles `CLAUDE.md`
per-worktree from ordered blocks via `Get-KilnRole`/`Get-KilnTemplate`/`Get-KilnConstitution`,
all passed through `Apply-Substitutions` (simple `{{TOKEN}}` string replace). The loop mechanic
lives *only* in `kiln/templates/loop-auto-claude.md`. Launch command
(`Build-WezTermAgentCommand`, ~line 697) has no `-p`/`--print`, no
`--allowedTools`/`--disallowedTools` — full Task/Agent tool already available.

**Why Unix needs new plumbing** — `bin/kiln.sh` does not mirror the Windows generation pipeline.
Confirmed by grepping the whole file: zero references to `kiln/templates/`. Instead
`write_agent_instruction_file()` (kiln.sh:429-444) writes a short prompt telling the agent to
recursively read `constitution.md` and `roles/<role>.md` itself. Neither of those files spells
out the receive→work→handoff→repeat loop anywhere (confirmed by reading `kiln/constitution.md`,
`kiln/constitution/workflow.md`, and `kiln/roles/coder.md` in full) — that mechanic exists
exclusively inside the Windows-only injected template. Additionally, `lib/profile-loader.sh`
never emits `TERMINAL_{i}_MODE` at all (confirmed by grep), even though
`lib/profile-loader.ps1:113-117` does and `kiln/profiles.json` already carries a `mode` field per
terminal — so `bin/kiln.sh` doesn't even know which roles are auto vs. manual today.

**Custom subagent facts**, confirmed against official Claude Code docs:

- `tools:` frontmatter is an allow-list; **`disallowedTools:` is a real deny-list** — use
  `disallowedTools: Agent, mcp__kiln-db, mcp__kiln-channel` rather than enumerating an allow-list.
  `mcp__<server>` excludes all of that server's tools.
- Skills **are** invocable inside custom subagents via the Skill tool without preloading — no
  need to inline `tdd-red`/`coverage-check`/etc. content into the worker body.
- `.claude/agents/*.md` discovery walks up from cwd at launch — a file at
  `<worktree>/.claude/agents/<role>-worker.md` is found correctly once the wrapper cd's into that
  worktree.

**`.gitignore` requirement, confirmed load-bearing by an existing incident on record**
(`bin/kiln.ps1:113-118`, comment + war story): `CLAUDE.md`, `.mcp.json`, and `tmp/` are
gitignored specifically *because* they're regenerated per-worktree/per-role with different
content each time — if tracked, every role's copy differs, so every `/kiln-receive` `git merge`
hits an add/add conflict on all three. The comment records this actually happened live: "an
agent got stuck exactly here and had to escalate instead of resolving it." A per-worktree
`<role>-worker.md` is exactly this same category of file, so it needs the same treatment: add
`.claude/agents/` to the same gitignore machinery that already handles
`CLAUDE.md`/`.mcp.json`/`tmp/` (`Ensure-InitialGitignore` in `kiln.ps1` + the template in
`kiln-init.ps1`, and the Unix equivalents).

**Pre-existing Unix gap surfaced by this check:** `kiln-init.sh`'s `.gitignore` template
(~line 249-272) and `kiln.sh`'s `ensure_initial_gitignore` (~line 116-135) only handle
`.kiln/`/`.worktrees/` — neither includes `CLAUDE.md`, `.mcp.json`, or `tmp/` at all, unlike
their Windows counterparts. Unix is currently exposed to the exact add/add conflict bug Windows
already fixed. Since this plan is already rewriting Unix's generation pipeline, fixing this gap
in the same pass (bring Unix's gitignore handling up to parity with Windows, covering
`CLAUDE.md`, `.mcp.json`, `tmp/`, and the new `.claude/agents/`) is in scope here rather than
deferred.

## Design

### 1. Windows: worker subagent generator

New function `Write-WorkerAgentFile` in `bin/kiln.ps1`, placed after `Write-GeneratedCLAUDEmd`
(~line 679), called right after the existing `Write-GeneratedCLAUDEmd` calls (~1132, ~1156),
guarded to `$Agent -eq "claude" -and $Mode -eq "auto"`:

- Writes `<worktree>/.claude/agents/<role>-worker.md`.
- Frontmatter: `name: <role>-worker`, a description noting it's dispatched only by the wrapper,
  `disallowedTools: Agent, mcp__kiln-db, mcp__kiln-channel`.
- Body: a new shared template `kiln/templates/worker-status.md` (the WORKER_STATUS contract,
  read via `Get-KilnTemplate`) + `Get-KilnRole $Role` (unmodified role file) + `project.md` +
  `engineering.md` constitution slices. **Not** `workflow.md` — that's handoff/branch-discipline
  protocol, the wrapper's concern, not the worker's.
- All blocks pass through the existing `Apply-Substitutions`.

**Slim the wrapper's own generated `CLAUDE.md`** (required, not optional — leftover content in
the wrapper reintroduces the exact ambiguity this plan removes): in `Write-GeneratedCLAUDEmd`,
when `Mode -eq "auto" -and $Agent -eq "claude"`:

- Replace `$roleBlock` with a short pointer ("your work is delegated to `<role>-worker`, do not
  do it yourself, see Step 2") instead of the full `Get-KilnRole` text.
- Drop `$constitutionBlock`, `$project`, and `$engineering` from the assembled `$blocks` array
  entirely. Final block list for auto+claude: `roleBlock (pointer)`, `loopBlock`, `runtimeBlock`,
  `workflow` only.

Copilot and manual-mode (specifier) wrappers are unaffected — full role block and full
constitution stack, unchanged, since they still do their own work.

**Add `.claude/agents/` to gitignore handling**: `Ensure-InitialGitignore` (`kiln.ps1` ~line 100)
gets a `needsClaudeAgents` check alongside the existing `needsClaudeMd`/`needsMcpJson`/`needsTmp`
checks; `kiln-init.ps1`'s `.gitignore` template (~line 255-283) gets the same line added for
fresh projects.

### 2. Unix: build the missing loop-injection pipeline (prerequisite, in-scope)

Three layers, bottom-up:

**a. Profile → mode.** Mirror `profile-loader.ps1`'s pattern in `lib/profile-loader.sh`: emit
`TERMINAL_{i}_MODE` from the profile's `mode` field. In `bin/kiln.sh`'s
`load_config_from_profile`, add a `MODES` array alongside the existing `ROLES`/`AGENTS` arrays.

**b. Bundled-templates path.** `bin/kiln.sh`'s existing `$KILN_DIR` is the *project's own copy*
(`$WORKING_DIR/kiln`, confirmed — populated by `kiln-init.sh`, which does not copy `templates/`).
Add `FRAMEWORK_ROOT`/`KILN_BUNDLED_DIR` the same way `kiln-init.sh:55-56` already does, pointing
at the framework's own `kiln/templates/`.

**c. Assembly + rewrite.** New functions in `bin/kiln.sh` next to `write_agent_instruction_file`:
`get_kiln_template`, `get_kiln_constitution`, `get_kiln_role` (bash/python port of the
Message-Loop-section-stripping regex), `apply_substitutions` (literal string replace, not regex,
to safely handle path-valued tokens like `{{DB_PATH}}`), `lookup_handoff_target` (parse the
Handoff Routing table out of `workflow.md`), `lookup_commit_format`. Rewrite
`write_agent_instruction_file` to assemble content via a new `render_kiln_blocks` helper, keeping
`kiln/templates/*.md` as the single source of truth so the two platforms can't drift:

- For `mode == "auto"` (claude, delegating to a worker): `role pointer → loop → runtime →
  workflow` only — mirrors the slimmed Windows output.
- For `mode == "manual"` (specifier) or non-claude agents: full block order as before
  (role → loop → runtime → constitution header → project → engineering → workflow).

`launch_role` passes the role's mode through. The launch command itself
(`--append-system-prompt-file` + initial message) is unchanged — only the file's content gets
richer (or, for auto+claude, slimmer than what Unix writes today).

New `write_worker_agent_file` (Unix), same shape as Windows' `Write-WorkerAgentFile`: writes
`<worktree>/.claude/agents/<role>-worker.md` with the same frontmatter/body composition. Called
from `launch_role`, same `claude` + `auto` guard.

**d. Gitignore parity fix.** `kiln-init.sh`'s `.gitignore` template and `kiln.sh`'s
`ensure_initial_gitignore` currently only handle `.kiln/`/`.worktrees/` — missing
`CLAUDE.md`/`.mcp.json`/`tmp/` entirely, unlike Windows. Bring both up to parity with
`kiln.ps1`/`kiln-init.ps1` (add all three) and add `.claude/agents/` alongside them in the same
pass.

**Known secondary gap to fix alongside this:** `bin/kiln.sh`'s `claude` launch command has no
`--debug-file` flag at all (confirmed — Unix currently has no equivalent to
`.kiln/logs/claude-debug-<role>.log`). Add one so the verification plan below can compare
context-growth logs on both platforms; small addition, same launch-command edit site.

### 3. `kiln/templates/loop-auto-claude.md` — Step 2 rewrite (shared by both platforms)

Step 2 changes from doing the role's work directly to:

- Invoke `Agent` tool, `subagent_type: "{{ROLE}}-worker"`, `run_in_background: false` (wrapper
  must block — steps 3/4 depend on the result). Prompt: the verbatim content of
  `tmp/handoff-in.md`, current branch/worktree, and the explicit instruction to end the final
  report with a literal `WORKER_STATUS: DONE` or `WORKER_STATUS: BLOCKED — <reason>` line. State
  explicitly: do not perform the work yourself, delegate it entirely.
- Check the worker's status line:
  - `DONE` → proceed to Step 3 with the worker's report as handoff content.
  - `BLOCKED`, first occurrence this cycle → re-dispatch the same worker once more, folding the
    failure reason into the prompt as feedback.
  - `BLOCKED` again (or no status line found) on the retry → stop retrying, go to Step 3 in
    **escalation mode**: same routing-table target as always, but the message body reports the
    blocker (both failure reasons) instead of normal work. If the worker made no commits, run an
    empty commit first so `/kiln-handoff`'s squash step has something to act on.
- Extend the existing "not end-of-turn" guardrail to explicitly cover "the subagent call has
  returned" — the new place a wrapper could plausibly stop early, alongside the existing
  coverage of the handoff-sent step.

New shared template `kiln/templates/worker-status.md` carries the WORKER_STATUS contract text
(consumed by both the Windows and Unix worker-file generators) plus a note that the worker has
full Skill-tool access to any Kiln skills its role references (`tdd-red`, `coverage-check`,
`crap-run`, etc.) — no need to inline skill content.

`loop-manual-claude.md` (specifier) is confirmed structurally different (human-approval step
between work and handoff) and stays unchanged — the worker-file generators are gated on
`Mode -eq "auto"` specifically so no `specifier-worker.md` is ever produced. Same pattern should
be directly reusable there later; out of scope for this plan.

## Files touched

| File           | Change |
| -------------- | ------ |
| `bin/kiln.ps1` | New `Write-WorkerAgentFile` + 2 call sites; `Write-GeneratedCLAUDEmd`'s auto+claude output slimmed to role-pointer + loop + runtime + workflow only; `Ensure-InitialGitignore` gets a `.claude/agents/` backfill check |
| `bin/kiln.sh` | New `KILN_BUNDLED_DIR`; new template/constitution/role/substitution/routing helper functions; rewritten `write_agent_instruction_file` (mode-aware block set, same slimming as Windows); new `write_worker_agent_file`; `MODES` array wiring; `--debug-file` added to the claude launch command; `ensure_initial_gitignore` extended to match Windows parity |
| `lib/profile-loader.sh` | Emit `TERMINAL_{i}_MODE` from the profile's `mode` field |
| `kiln-init.ps1` | `.gitignore` template gets `.claude/agents/` added |
| `kiln-init.sh` | `.gitignore` template gets `CLAUDE.md`, `.mcp.json`, `tmp/`, `.claude/agents/` added (parity fix + new entry) |
| `kiln/templates/loop-auto-claude.md` | Step 2 rewritten (dispatch/retry/escalate); guardrail extended |
| `kiln/templates/worker-status.md` | **New** — shared WORKER_STATUS contract, read by both platforms |
| `kiln/roles/*.md` (coder, refactorer, architect, reviewer, selftest) | Content unchanged, now also consumed as worker-agent bodies |
| `kiln/templates/loop-manual-claude.md` | No change |

No changes needed: `.mcp.json` generation, `kiln/.claude/settings.json` (no Task/Agent entries
today; `bypassPermissions`/`acceptEdits` already cover it), constitution file contents,
`Read-HandoffRoutingTable`.

## Verification / rollout plan

1. **Static check, both platforms**: dry-run each launcher against a scratch project; confirm
   `.claude/agents/<role>-worker.md` is generated for every `claude`+`auto` role and *not* for
   specifier (manual) or copilot roles; diff the generated `CLAUDE.md`/prompt file to confirm it's
   now just role-pointer + loop + runtime + workflow for auto+claude, and unchanged for
   copilot/manual; confirm `.gitignore` contains `.claude/agents/` (and, on Unix, the newly-added
   `CLAUDE.md`/`.mcp.json`/`tmp/` parity entries) after init.
2. **`selftest` profile, Windows then Unix** — validates the mechanical plumbing (worker
   generation, dispatch, retry/escalation, and — new on Unix — the whole template-assembly path)
   before trusting it with real TDD work. Since selftest messages are intercepted at
   `/kiln-receive` Step 3 before reaching Step 2, explicitly also trigger one non-selftest
   handoff into `coder` in this run to exercise the dispatch path.
3. **Multi-cycle run against `examples/library-hub`**, on both platforms: 4-5 full
   specifier→coder→refactorer→architect cycles per platform, watching specifically for the
   original failure mode (dropped handoff / failure to resume listening).
4. **Cross-check logs on both platforms**: `.kiln/logs/claude-debug-<role>.log` (now available on
   Unix too, per the `--debug-file` addition) and `logbook.md`, for fewer tool-call entries
   between `[RECEIVED]` and `[SENT]` per cycle, and correct `WORKER_STATUS` markers in the
   worker's returned report.
5. **Force one `BLOCKED` cycle** (e.g., hand the coder-worker a deliberately malformed handoff) to
   confirm: retry-with-feedback fires once, escalation on the second failure routes to the normal
   target with blocker content, and the empty-commit fallback works when the worker made no
   changes.
6. Only after selftest + library-hub pass on both platforms, consider extending the pattern to
   the specifier's manual loop.
