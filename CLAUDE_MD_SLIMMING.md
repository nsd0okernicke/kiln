# CLAUDE.md Slimming for Auto-Mode Shells

## Problem

The generated `CLAUDE.md` files for auto-mode shell agents were bloated with content meant for the worker subagents:

```
Old CLAUDE.md contents:
├─ Role block (full TDD workflow, responsibilities, quality gates)
├─ Loop template (message loop)
├─ Runtime config
├─ Constitution header
├─ Project rules
├─ Engineering standards
└─ Workflow routing rules
```

**Issue**: The shell agent doesn't do any of the work described in the role block. It only listens, delegates, and sends. Including TDD rules, test strategies, and implementation patterns (all meant for the worker) was:

1. **Confusing** — Shell sees role rules but can't/shouldn't follow them
2. **Wasteful** — ~400 lines of context per shell agent that serve no purpose
3. **Risky** — Shell might see role description and try to do some work itself, breaking the delegation model
4. **Misaligned** — Architectural intent is "thin shells" but implementation was "fat shells"

## Solution

### 1. Exclude Role Block from Auto-Mode Shells

**Change**: `bin/kiln.ps1` + `bin/kiln.sh`

For Claude agents in `auto` mode:
```python
# OLD:
blocks = [roleBlock, loopBlock, runtimeBlock, constitutionBlock, project, engineering, workflow]

# NEW:
blocks = [shellPrompt, loopBlock, runtimeBlock, constitutionBlock, project, engineering, workflow]
# (exclude roleBlock, add shellPrompt instead)
```

**Effect**: Shell agents no longer see:
- TDD cycle rules
- Test strategies
- Code organization patterns
- Quality gate requirements
- Any implementation-specific guidance

All that content is now **only** in the worker subagent definition.

### 2. Add Shell-Prompt Template for Auto-Mode

**New file**: `kiln/templates/shell-prompt-auto-claude.md`

```markdown
# Shell Agent — Message Loop Only

**Your role: LISTEN → DELEGATE → SEND. Nothing else.**

Do not do any of the {{ROLE_UPPER}} work yourself. You are a thin shell that:

1. Listens for messages via `/kiln-receive`
2. Delegates all work to the `{{ROLE}}-worker` subagent
3. Sends completed work via `/kiln-handoff`
4. Repeats

The worker subagent has all the {{ROLE}} role rules, quality gates, and standards baked in. Your CLAUDE.md only contains the message loop — the constitution and workflow rules you need to route messages correctly.

**Do not skip to your role section and start implementing.** There is none. All {{ROLE}} work rules are in the worker subagent definition (`.claude/agents/{{ROLE}}-worker.md`), not in this file.
```

**Purpose**: Explicitly tell the shell agent what it should NOT do, and where to find what it should NOT replicate.

### 3. Update Unix Instruction File

**Change**: `bin/kiln.sh` `write_agent_instruction_file()` function

Unix agents receive a simpler instruction file (they fetch content dynamically). Updated it to:

```markdown
# Shell Agent — Message Loop Only

**Your role: LISTEN → DELEGATE → SEND. Nothing else.**

Do not do any of the ${role^^} work yourself. You are a thin shell that:
1. Listens for messages via `/kiln-receive`
2. Delegates work to the `${role}-worker` subagent via Agent tool
3. Sends completed work via `/kiln-handoff`
4. Repeats

The worker subagent has all the ${role} role rules, quality gates, and standards. Your job is the message loop only.

Read Kiln/constitution.md for workflow and routing rules.
Read Kiln/.claude/agents/${role}-worker.md to see what the worker subagent does (do not replicate it yourself).
```

**Purpose**: Same as shell-prompt — clarify delegation model upfront.

## Impact on Generated Files

### Before

A typical `CLAUDE.md` for coder shell might look like:

```
CODER ROLE
- Implement in project language
- Own implementation of approved behavior
- Start from latest specification
- Implement step definitions for acceptance tests
- Run complete TDD cycle without pausing
- For each behavior: tdd-red → tdd-green → tdd-refactor
- Write domain unit tests first
- Write application unit tests
- Implement production code
- Wire infrastructure last
- Do not rely on acceptance tests
- Keep new behavior in testable modules
- Run property tests only when requested
- Keep implementation code understandable
- Leave broad cleanup to refactorer
- Do not run mutation, CRAP, or DRY checks
- Do not run Gherkin acceptance mutation

---

MESSAGE LOOP
[receive/delegate/handoff cycle]

---

RUNTIME CONFIGURATION
[config details]

---

CONSTITUTION: ... [project/engineering/workflow rules]
```

**Line count**: ~600 lines

**Problem**: Shell agent sees all TDD rules but:
- Can't implement them (work is delegated)
- Shouldn't implement them (breaks delegation model)
- Will be confused by them

### After

The same `CLAUDE.md` now looks like:

```
SHELL AGENT — MESSAGE LOOP ONLY
Your role: LISTEN → DELEGATE → SEND. Nothing else.

Do not do any of the CODER work yourself. You are a thin shell that:
1. Listens for messages via `/kiln-receive`
2. Delegates all work to the `coder-worker` subagent
3. Sends completed work via `/kiln-handoff`
4. Repeats

The worker subagent has all the CODER role rules, quality gates, and standards baked in.
Your CLAUDE.md only contains the message loop — the constitution and workflow rules you need
to route messages correctly.

Do not skip to your role section and start implementing. There is none.
All CODER work rules are in the worker subagent definition (`.claude/agents/coder-worker.md`), not in this file.

---

MESSAGE LOOP
[receive/delegate/handoff cycle]

---

RUNTIME CONFIGURATION
[config details]

---

CONSTITUTION: ... [project/engineering/workflow rules only, no role]
```

**Line count**: ~200 lines

**Benefit**: 
- ✅ Clear about what shell should do
- ✅ No confusing role rules
- ✅ No unnecessary context
- ✅ Aligns with "thin shell" architecture

## What Shell Still Has

After slimming, the shell CLAUDE.md still includes:

1. **Shell-prompt** ← NEW, clarifies delegation
2. **Loop template** — Essential (defines the cycle)
3. **Runtime config** — Essential (paths, database, etc.)
4. **Constitution header** — Reference
5. **Project rules** — Useful (project constraints)
6. **Engineering standards** — Reference
7. **Workflow rules** — Essential (who routes to whom)

**Why these are kept**:
- Loop template: Only thing shell actually does
- Runtime/project/workflow: Shell needs these to route messages and understand constraints
- Engineering: General reference, helps shell understand project standards (even if not implementing)

**What's removed**:
- **Role block**: Not needed (work is delegated)
- TDD rules: Not needed (worker has them)
- Implementation patterns: Not needed (worker has them)
- Test strategies: Not needed (worker has them)

## For Specifier (Manual Mode)

The specifier role uses `manual` mode, not `auto` mode. It still receives the full role block because specifier actually does work (writing specifications, approving work). The specifier's CLAUDE.md was not changed.

## Testing

To verify the slimming works:

1. **Generate a new shell CLAUDE.md**:
   ```bash
   ./bin/kiln.ps1 --profile dev /path/to/test
   ```

2. **Check file size**:
   ```bash
   wc -l .worktrees/coder/CLAUDE.md
   # Should be ~150-250 lines (was ~600)
   ```

3. **Verify content**:
   ```bash
   grep -c "TDD\|tdd-red\|tdd-green" .worktrees/coder/CLAUDE.md
   # Should be 0 (no TDD rules in shell)
   ```

4. **Check for clarity**:
   ```bash
   grep -A 5 "Shell Agent" .worktrees/coder/CLAUDE.md
   # Should see clear "LISTEN → DELEGATE → SEND" statement
   ```

## Commits

- **0ef2a04** — Slim down generated CLAUDE.md for auto-mode shell agents

## References

- Architecture: `plan-subagent-shells.md` (section "Shell Agent Architecture")
- Loop template: `kiln/templates/loop-auto-claude.md`
- Shell prompt template: `kiln/templates/shell-prompt-auto-claude.md` (new)

## Next Steps

1. Generate and review a new CLAUDE.md from a fresh test run
2. Verify shell agents still work correctly (loop executes properly)
3. Monitor shell context growth (should be minimal)
4. Consider similar slimming for Copilot instructions if applicable
