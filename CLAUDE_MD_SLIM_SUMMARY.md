# Shell CLAUDE.md Slimming Summary

**Commit**: `279ce12`  
**Date**: 2026-07-26  
**Scope**: Windows (`kiln.ps1`) code generation + template cleanup

---

## Problem

Generated shell `CLAUDE.md` files for auto-mode roles (coder/refactorer/architect) were ~186 lines, when the shell's only job is "LISTEN → DELEGATE → SEND":
1. Listen for a message via `/kiln-receive`
2. Delegate work to a worker subagent (`.claude/agents/<role>-worker.md`)
3. Send the result via `/kiln-handoff`
4. Loop back to step 1

The file contained much content irrelevant to this thin message loop.

---

## Root Cause Analysis

The shell's `CLAUDE.md` was assembled from these blocks (via `Write-GeneratedCLAUDEmd`, `kiln.ps1:641`):
- `wrapper-prompt-auto-claude.md` (9 lines) — "LISTEN → DELEGATE → SEND, nothing else" ✅ keep
- `loop-auto-claude.md` (28 lines) — the message loop structure ✅ keep
- `runtime-claude.md` (6 lines) — resolved Role/Branch/Worktree/DB values ✅ keep
- `constitution.md` (11 lines) — precedence preamble (moot for shells) ✅ **remove**
- **`project.md` (4 lines)** — language, project rules ✅ **remove** — duplicated in worker
- **`engineering.md` (15 lines)** — toolchain, testing practices ✅ **remove** — duplicated in worker
- `workflow.md` (71 lines) — message queue, handoff format, routing ✅ keep, but trim

### Duplication Analysis

Both the shell and the worker agent file (`Write-GeneratedWorkerAgent`, line 700) independently loaded and merged:
- `project.md` (4 lines)
- `engineering.md` (15 lines)

The shell never executes these rules (it doesn't run tests, configure toolchains, or change code). Only the worker does. These 19 lines were pure duplication.

### Workflow.md Redundancy

The workflow.md in the generated file had two verbose sections:
1. **Commit Convention** — listed example commits for all four roles (`[Coder]`, `[Refactorer]`, etc.), but a shell only needs its own role's format. A `{{COMMIT_FORMAT}}` substitution already existed in `kiln.ps1:679` (and was already used by Copilot templates) but was never used by Claude templates.
2. **Message Queue** — spent ~7 lines explaining how to discover your worktree and branch by walking up parent directories. But `runtime-claude.md` already declares the resolved `Worktree:` and `Branch:` in the same generated file, making this discovery prose redundant.

---

## Changes Made

### 1. `bin/kiln.ps1` — `Write-GeneratedCLAUDEmd` (lines 682–690)

**Before:**
```powershell
if ($Mode -eq "auto" -and $Agent -eq "claude") {
    $blocks = @($wrapperPromptBlock, $loopBlock, $runtimeBlock, $constitutionBlock, $project, $engineering, $workflow) | Where-Object { $_ }
} else {
    $blocks = @($roleBlock, $loopBlock, $runtimeBlock, $constitutionBlock, $project, $engineering, $workflow) | Where-Object { $_ }
}
```

**After:**
```powershell
if ($Mode -eq "auto" -and $Agent -eq "claude") {
    $blocks = @($wrapperPromptBlock, $loopBlock, $runtimeBlock, $workflow) | Where-Object { $_ }
} else {
    $blocks = @($roleBlock, $loopBlock, $runtimeBlock, $constitutionBlock, $project, $engineering, $workflow) | Where-Object { $_ }
}
```

**Effect**: 
- Auto-mode shells drop `$constitutionBlock`, `$project`, and `$engineering` entirely.
- Manual-mode roles (specifier) keep all blocks (specifier still does its own work, needs full context).
- Worker agent files unaffected — they still get role + project + engineering via `Write-GeneratedWorkerAgent`.

### 2. `kiln/constitution/workflow.md` — Trim redundant content

#### Removed from Message Queue section:
```
- The project root is the directory containing `.kiln/`. From a named worktree (e.g., `.worktrees/coder/`), walk up parent directories until you find it.
- At startup, discover and remember the branch or worktree assigned to your role.
- If your assigned worktree is `@current`, `master`, or `none`, work in the main project checkout on its current branch; do not expect or create a `.worktrees/<role>` directory for that role.
- When one role has `@current` and another has a named worktree (e.g., `coordinator` with `@current` and `coder` with `coder`): the `@current` role works in the main directory on the current branch, while the named-worktree role works in `.worktrees/coder` on a sub-branch named `<current-branch>-coder`. Both roles see the same HEAD branch, but from different worktrees and branches.
```

**Rationale**: All this discovery logic is moot for generated (as opposed to hand-written) files, since `runtime-claude.md` already provides the resolved values.

#### Replaced in Commit Convention section:
**Before:**
```
**Format:** `[Role] Brief description - what was done`

Examples:

- `[Coder] Implement user registration - TDD for POST /users with email validation`
- `[Refactorer] Quality gates pass - CRAP ≤ 6, 91% coverage, DRY scan clean`
- `[Architect] Module boundaries aligned - split order_processor into command/query modules`
- `[Specifier] Accept registration story - Gherkin for email, duplicate, and empty-name cases`
```

**After:**
```
**Format:** `{{COMMIT_FORMAT}}`
```

**Rationale**: The placeholder `{{COMMIT_FORMAT}}` is already substituted with the correct role-specific format during generation (e.g., `[Coder] TDD implementation of <what>` for the coder shell). Removes 4 example lines while keeping the rule intact.

#### Reorganized Message Queue section for clarity:
Grouped related bullets into named sub-sections:
- **Worktree & Branch** — assignment and scope constraints
- **Handoff Mechanics** — SQLite, message format, routing rules

---

## Line Count Impact

| File | Before | After | Change |
|------|--------|-------|--------|
| `workflow.md` | 71 | 46 | -25 lines |
| Generated shell `CLAUDE.md` (total) | ~186 | ~99 | -87 lines (47% reduction) |

### Breakdown of the 186-line original:
- ~65 lines: wrapper + loop + runtime (kept as-is) ✅
- ~11 lines: constitution preamble ❌ **removed**
- ~19 lines: project + engineering (duplicated with worker) ❌ **removed**
- ~71 lines: workflow (trimmed to 46) ✅ **45 lines kept, 26 removed**

### Generated new shell `CLAUDE.md` (~99 lines):
- wrapper-prompt (9) + loop (28) + runtime (6) + workflow (46) = 89 lines
- Plus 2-line header + ~8 separator lines = ~99 lines total

---

## Verification Checklist

✅ **Content checked**:
- Handoff Message Format template still present in full (required by `/kiln-handoff` SKILL Step 3)
- Handoff Routing table still present (required by `/kiln-handoff` SKILL)
- Commit format now via `{{COMMIT_FORMAT}}` substitution (already used by Copilot templates, now available to Claude)
- Message Queue priority values, DB access, and scoping rules all intact

✅ **No load-bearing content removed**:
- Consulted `kiln/skills/kiln-handoff/SKILL.md` and `kiln/skills/kiln-receive/SKILL.md` to verify what they say they read "from context"
- All those references still present in the generated file

✅ **Worker agent files unaffected**:
- `Write-GeneratedWorkerAgent` still generates `.claude/agents/<role>-worker.md` with role + project + engineering
- Worker remains the only place doing implementation work, with full context

✅ **Manual-mode roles unaffected**:
- Specifier (and any manual-mode role) keeps full constitution + project + engineering + workflow in its CLAUDE.md
- Specifier does its own work, so needs all the context

---

## Next Steps

1. **Run `selftest` profile** to verify the shell + worker delegation loop still works end-to-end with the slimmed CLAUDE.md
2. **Run a multi-cycle LibraryHub test** to confirm shells complete full loops and context doesn't accumulate
3. **Inspect generated CLAUDE.md** in a fresh project to eyeball the ~99-line file and confirm nothing essential is missing

---

## Impact on Other Platforms

**Unix/macOS (`kiln.sh`)**: No changes needed. The Unix launcher doesn't do CLAUDE.md template assembly (confirmed — no `write_agent_instruction_file` equivalent exists that builds from blocks). This slimming applies only to Windows.

**Copilot agents**: Unaffected. They were already using `loop-auto-copilot.md` which references commit format and workflow rules via inclusion, and the changes maintain those.

---

## Key Learnings

1. **Duplication detection**: When two subagents (shell + worker) both independently include the same content, it's a sign that content belongs in only one place — the worker, since that's who needs it.
2. **Placeholder reuse**: The `{{COMMIT_FORMAT}}` placeholder existed but was underutilized. Moving to it for workflow.md makes it available to all agents, not just Copilot.
3. **Generated files vs. hand-written**: Discovery prose (walking up directory trees, checking `@current`) makes sense in a *hand-written* prompt but is redundant in a *generated* one where values are already resolved.
