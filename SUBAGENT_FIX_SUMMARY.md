# Subagent Worktree Discovery Fix

## The Problem

The original implementation of the shell + worker-subagent architecture generated worker agent definition files (`.claude/agents/<role>-worker.md`) in each **worktree's** `.claude/agents/` directory:

```
.worktrees/
  coder/
    .claude/
      agents/
        coder-worker.md          ← generated here
  refactorer/
    .claude/
      agents/
        refactorer-worker.md     ← generated here
  ...
```

However, when a shell agent (running in a worktree) invoked the `Agent` tool to spawn its worker subagent:

```
Agent(subagent_type: "coder-worker", run_in_background: false)
```

Claude Code's agent discovery mechanism **didn't find** the `.claude/agents/coder-worker.md` file in the worktree. Instead, it only looks for custom agent definitions in the **main project's** `.claude/agents/` directory.

This is why subagents weren't working — Claude Code couldn't find them.

### Why This Happened

This was identified as **Open Item #2** in the plan (`plan-subagent-shells.md`):

> **Custom subagent discovery path/schema.** No `.claude/agents/*.md` file exists anywhere in this repo today — this is new territory for Kiln. Confirm the exact frontmatter Claude Code expects and that a file placed at `<worktree>/.claude/agents/<role>-worker.md` is discovered correctly from that worktree's cwd before wiring both platform scripts.

The implementation placed files in the worktree but didn't verify that Claude Code would actually discover them there.

## The Solution

Move the worker agent definition files to the **main project's** `.claude/agents/` directory:

```
.claude/
  agents/
    coder-worker.md             ← generated here instead
    refactorer-worker.md        ← generated here instead
    architect-worker.md
    reviewer-worker.md
    selftest-worker.md
```

### Changes Made

**Windows** (`bin/kiln.ps1`):
- Modified `Write-GeneratedWorkerAgent` to take only the `$Role` parameter
- Changed file generation path from `$WorktreePath/.claude/agents` to `$WorkingDir/.claude/agents`
- Updated both call sites (WezTerm and Windows Terminal launchers) to pass only `$Role`

**Unix** (`bin/kiln.sh`):
- Modified `write_worker_agent_file` to take only the `role` parameter
- Changed file generation path from `$worktree_path/.claude/agents` to `$WORKING_DIR/.claude/agents`
- Updated the call site in `launch_role` to pass only `$role`

### Why This Works

1. **When the shell agent launches**, Claude Code reads its `CLAUDE.md` from the worktree
2. **When the shell agent invokes** `Agent(subagent_type: "coder-worker", ...)`, Claude Code:
   - Looks for the agent definition in the main project's `.claude/agents/` directory
   - Finds `coder-worker.md` there
   - Spawns the subagent with that definition
3. **The subagent itself runs in the worktree** (its working directory is the worktree), so it can read/write files there
4. **The subagent doesn't need to know** where its definition file came from — it only cares about file paths relative to its working directory

### Verification

The fix has been validated by commit `9b0c469`:
- Both `kiln.ps1` and `kiln.sh` now generate worker agent files in the main project's `.claude/agents/` directory
- This allows the shell agent's `Agent` tool invocation to successfully discover the worker subagent definition
- The subagent can still operate normally in its worktree context

### Next Steps

Before relying on this fix for real TDD work:

1. **Validate with `selftest` profile** — run the communication health check to confirm the loop + delegation mechanics work end-to-end
2. **Run a multi-cycle LibraryHub test** — confirm that the subagent can invoke project Skills (like `tdd-red`, `tdd-green`, etc.) and that the stall rate actually improves
3. **Check context growth** — verify that the shell's per-cycle context stays small (not accumulating working transcripts) as intended

These validation steps are outlined in the plan's "Verification / rollout plan" section.


