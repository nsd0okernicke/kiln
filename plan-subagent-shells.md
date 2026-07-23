# Shell + Worker-Subagent Architecture for Kiln Role Agents

## Context

Kiln's auto-loop role agents (coder, refactorer, architect, reviewer, selftest — specifier is
manual and out of scope for now) run a persistent cycle: listen for a handoff → merge the
sender's worktree → do their role's work → commit → send a handoff → listen again. After several
cycles, agents sometimes stop sending the handoff or fail to resume listening — the loop silently
stalls. `TODO.md`'s Track A/B/C already track hardening attempts for this (prompt hardening,
`Stop` hooks, a watcher process).

Investigation confirmed the likely dominant contributor: steps 1 (listen), 2 (merge), 4 (commit),
5 (send) are **already fully mechanical**, encapsulated in the `/kiln-receive` and `/kiln-handoff`
skills (`kiln/skills/kiln-receive/SKILL.md`, `kiln/skills/kiln-handoff/SKILL.md`). The *only* step
that runs as open-ended, many-tool-call agent work inside the same long-lived process is step 3
("apply your role rules" — `kiln/templates/loop-auto-claude.md:13`). That work (reading files,
iterating code, running test suites) is what fills up the shell's context turn over turn and
dilutes the comparatively small, static receive/handoff instructions that are responsible for the
steps that go missing.

**The fix:** turn each role's persistent process into a thin shell that only ever does
listen → merge → dispatch a disposable subagent for the actual work → commit → send → listen
again. The subagent's full working transcript never enters the shell's context — only its final
report does — so the shell's per-cycle context stays small and repetitive, keeping the
receive/handoff instructions proportionally dominant every cycle instead of being crowded out.

This was confirmed technically feasible, not just plausible:
- Agents are launched **interactively**, not headless: `claude --model <M> --permission-mode
  bypassPermissions --mcp-config ./.mcp.json ...` (Windows, `bin/kiln.ps1` ~line 697) /
  `claude --mcp-config ./.mcp.json --append-system-prompt-file <f> --permission-mode acceptEdits
  ...` (Unix, `bin/kiln.sh` ~line 475). No `-p`/`--print`, no `--allowedTools`/`--disallowedTools`
  anywhere — the full Agent/Task tool is already available to every role agent.
- Precedent already exists in this repo: `kiln/skills/review/SKILL.md` dispatches two parallel
  `general-purpose` subagents and aggregates their reports ("Both axes run as parallel sub-agents
  so they don't pollute each other's context").
- `TODO.md` Track C independently names almost this exact design as the long-term reliability
  escalation path ("agent only executes the task and signals completion... agents become smart
  task executors rather than full workflow owners"), which is corroborating evidence this
  direction is sound, not a novel guess.

User decisions locked in for this plan:
- Apply to **all** auto-loop roles at once (coder, refactorer, architect, reviewer, selftest).
  Specifier stays manual/unchanged.
- Worker briefing mechanism: **generate a custom per-role subagent definition file** at launch
  time (mirrors the existing CLAUDE.md generation pipeline), rather than reusing the generic
  `general-purpose` type with role instructions inlined into every dispatch prompt.
- Failure handling: if the worker reports it couldn't finish, **retry once with the failure
  report as feedback**; if it fails again, escalate — send a handoff whose content reports the
  blocker instead of normal work, so the loop never silently stalls.

## Design

### New artifact: per-role worker subagent definition

At the same point `Write-GeneratedCLAUDEmd` runs (`bin/kiln.ps1:627`, called from `kiln.ps1:1132`
and `:1156`), also generate `.claude/agents/<role>-worker.md` in the worktree — a custom Claude
Code subagent type built from the **same primitives already used** (`Get-KilnTemplate`,
`Get-KilnRole`, `Get-KilnConstitution`, `Apply-Substitutions`), just assembled differently:

- **Frontmatter**: `name: <role>-worker`, a description, `tools:` excluding `Agent` (no recursive
  subagent spawning) and excluding the `kiln-db`/`kiln-channel` MCP tools (the worker never
  touches messaging — that stays the shell's job).
- **Body**: the unmodified role file (`kiln/roles/<role>.md`) + `engineering.md` + `project.md`
  constitution slices. **Not** `workflow.md` — that's handoff/branch-discipline protocol, the
  shell's concern, not the worker's.

Add a mirrored `write_worker_agent_file` (naming TBD to match existing `bin/kiln.sh` conventions)
next to the existing instruction-file writer in `bin/kiln.sh` (~line 475's `launch_role` /
surrounding instruction-writing function). Same content; the worker file itself is a plain
`.claude/agents/*.md`, independent of which platform launched the shell process.

Only for `claude`-agent roles — Copilot's separate polling loop (no blocking channel, no
Task/Agent-tool subagents in the same sense) is untouched by this change.

### Loop template change (`kiln/templates/loop-auto-claude.md`)

Step 2 changes from doing the role's work directly to:

- Invoke `Agent` tool with `subagent_type: "{{ROLE}}-worker"`, `run_in_background: false` (the
  shell must block here — steps 3/4 depend on the result). Prompt carries only cycle-specific
  material: the handoff content already persisted at `tmp/handoff-in.md`, current
  branch/worktree, and an explicit ask for a final report of what was implemented/verified and
  which files were touched. Explicitly state: do not perform the work yourself, delegate it
  entirely.
- **Failure handling**: if the worker's report indicates it could not complete (blocked, failing
  tests, unclear task), re-dispatch once more with the failure report folded into the prompt as
  feedback. If it fails a second time, skip the normal handoff content and instead send a handoff
  through `/kiln-handoff` whose body reports the blocker — mirrors the existing special-case
  pattern already used for `system-communication-test` messages in `/kiln-receive` Step 3
  (`kiln/skills/kiln-receive/SKILL.md:26-30`).
- Extend the existing "not end-of-turn" guardrail (`loop-auto-claude.md:3-5`) to explicitly cover
  "the subagent call has returned" the same way it already covers the handoff-sent step — this is
  the new place a shell could plausibly stop early.

`loop-manual-claude.md` (specifier) is unchanged for now, per the user's original scope framing;
the same pattern should be directly reusable there later since the specifier's role file is
similarly short.

### No changes needed

- `.mcp.json` generation and `kiln/.claude/settings.json` — no MCP wiring or explicit tool
  allow/deny entries reference Task/Agent today, and none are required: `bypassPermissions` /
  `acceptEdits` already cover it, and no `--allowedTools`/`--disallowedTools` flag exists in any
  launch command.

## Files touched (representative)

- `bin/kiln.ps1` — new worker-file generation function + 2 call sites near `Write-GeneratedCLAUDEmd`
- `bin/kiln.sh` — Unix equivalent near `launch_role`/instruction-file writer (~line 475)
- `kiln/templates/loop-auto-claude.md` — Step 2 rewritten; guardrail wording extended; retry/escalate step added
- `kiln/roles/*.md` (coder, refactorer, architect, reviewer, selftest) — content unchanged, now also consumed as worker-agent bodies

## Open items to verify empirically during implementation (not resolvable by reading code)

1. **Skill invocability inside the worker subagent.** Role files reference Kiln Skills (`tdd-red`,
   `tdd-green`, `coverage-check`, `crap-run`, `mutation-testing`, etc., all under `kiln/skills/`).
   Unconfirmed whether a Task-tool-spawned custom subagent gets the same Skill-tool discovery as
   the top-level session it's spawned from. If not, the referenced skill content needs to be
   inlined into the worker's `.md` body rather than assumed reachable via `/skill-name`.
2. **Custom subagent discovery path/schema.** No `.claude/agents/*.md` file exists anywhere in
   this repo today — this is new territory for Kiln. Confirm the exact frontmatter Claude Code
   expects and that a file placed at `<worktree>/.claude/agents/<role>-worker.md` is discovered
   correctly from that worktree's cwd before wiring both platform scripts.
3. **Whether to trim the role block out of the shell's own `CLAUDE.md`.** Today
   `Write-GeneratedCLAUDEmd` still includes the full role block (`kiln.ps1:670`). Once (1) and (2)
   are confirmed, recommend trimming it to a short "your work is delegated to `<role>-worker`, do
   not do it yourself" pointer — leaving the full role text in the shell's own instructions
   re-introduces the exact ambiguity this change is meant to remove.

## Verification / rollout plan

1. Implement and validate against the existing `selftest` profile first (the project's standard
   mechanism for messaging/loop changes — see README "Communication System Health Check" and
   `TODO.md`'s testing note) to confirm the mechanical plumbing (worker generation, dispatch,
   retry/escalation) before trusting it with real TDD work.
2. Run a multi-cycle swarm against the `examples/library-hub` reference project end-to-end for at
   least 4-5 full specifier→coder→refactorer→architect cycles, specifically watching for the
   original failure mode (dropped handoff / failure to resume listening) to see whether it
   measurably drops versus today's baseline.
3. Cross-check `.kiln/logs/claude-debug-<role>.log` and `logbook.md` across those cycles to
   confirm the shell's context growth per cycle is visibly smaller (fewer tool-call entries
   between receive and handoff) than before.
4. Only after selftest + library-hub validation, consider extending the same pattern to the
   specifier's manual loop.
