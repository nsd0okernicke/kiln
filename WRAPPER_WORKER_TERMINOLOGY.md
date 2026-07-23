# Wrapper/Worker Terminology

## Why We Renamed "Shell" to "Wrapper"

"Shell" was confusing because it already means many things in software:
- Unix shell / shell scripting
- Shell commands
- Shell escape / shell injection
- Command shell

**"Wrapper"** is clearer and more descriptive:
- Accurately describes what the agent does: wraps input, delegates to worker, unwraps output
- Complements "worker" terminology: wrapper/worker is a clear pattern
- No overloaded meaning in software
- Parallels dependency injection and decorator patterns

## Architecture: Wrapper + Worker Pattern

```
┌─────────────────────────────────────────────┐
│  Persistent Wrapper Agent (thin shell)      │
├─────────────────────────────────────────────┤
│ • Listens for messages via /kiln-receive    │
│ • Delegates work to worker via Agent tool   │
│ • Sends results via /kiln-handoff           │
│ • Loop repeats indefinitely                 │
│                                             │
│ Small, focused context (~200 lines CLAUDE.md)
│ Keeps receive/handoff instructions dominant │
└────────────────┬────────────────────────────┘
                 │
                 │ Dispatches (Agent tool)
                 │ run_in_background: false
                 │
                 ▼
┌─────────────────────────────────────────────┐
│  Disposable Worker Subagent (smart executor) │
├─────────────────────────────────────────────┤
│ • Receives handoff + context from wrapper   │
│ • Runs complete role workflow               │
│ • TDD cycles, quality gates, testing        │
│ • Returns final report of work done         │
│                                             │
│ Full context + capabilities for the job     │
│ Lifecycle: task-scoped (created → completes)│
└─────────────────────────────────────────────┘
```

## Key Differences

| Aspect | Wrapper | Worker |
|--------|---------|--------|
| **Lifetime** | Persistent (loop runs indefinitely) | Disposable (created per task) |
| **Job** | Route messages, delegate, coordinate | Execute role-specific work |
| **Context** | Thin (~200 lines) | Rich (full role + constitution) |
| **Scope** | Message loop infrastructure | Single handoff cycle |
| **Focus** | Reliability of message flow | Correctness of implementation |
| **Responsibilities** | Listen → delegate → send | Do the actual work (TDD, tests, etc.) |
| **Context Growth** | Stays constant (thin shell stays thin) | Fresh each cycle (no accumulation) |

## What Stayed the Same

- Architecture and design patterns
- Code behavior
- Functionality
- Test cases
- Commit history (only new commits use "wrapper")

## What Changed

- **Terminology** in code and documentation
- **Template filename**: `shell-prompt-auto-claude.md` → `wrapper-prompt-auto-claude.md`
- **Variable names**: `$shellPromptBlock` → `$wrapperPromptBlock`
- **Comments** and documentation
- **Generated text** in CLAUDE.md files

## Files Updated

### Code Changes
- `bin/kiln.ps1` — `$wrapperPromptBlock`, comments
- `bin/kiln.sh` — instruction text mentions "Wrapper Agent"
- `kiln/templates/wrapper-prompt-auto-claude.md` — template content
- `kiln/templates/loop-auto-claude.md` — comments

### Documentation
- `plan-subagent-shells.md` — explains wrapper/worker pattern
- `CLAUDE_MD_SLIMMING.md` — wrapper-specific documentation
- `SESSION_SUMMARY.md` — references to wrapper agents
- `FINDINGS_AT_A_GLANCE.md` — wrapper terminology
- Other documentation files

## Usage in Code and Docs

### How to Refer to Agents

**Correct**:
- "The wrapper agent listens for messages"
- "The worker subagent executes the TDD cycle"
- "Wrapper/worker pattern"
- "Wrapper delegating to worker"

**Avoid** (old terminology):
- "The shell agent" (confusing)
- "The role agent" (too vague, could mean wrapper or worker)

### In Generated Files

Users will see in their generated `CLAUDE.md`:

```markdown
# Wrapper Agent — Message Loop Only

**Your role: LISTEN → DELEGATE → SEND. Nothing else.**

Do not do any of the CODER work yourself. You are a thin wrapper that:

1. Listens for messages via `/kiln-receive`
2. Wraps/delegates all work to the `coder-worker` subagent
3. Sends completed work via `/kiln-handoff`
4. Repeats
```

## Related Concepts

- **Message Loop**: The cycle that wrappers execute
- **Delegation**: How wrappers delegate to workers
- **Worktree**: Where wrappers and workers operate
- **Subagent**: Claude Code's term for dynamically spawned agents (workers)

## References

- Architecture plan: `plan-subagent-shells.md` (uses "wrapper/worker" terminology)
- Template: `kiln/templates/wrapper-prompt-auto-claude.md`
- Commit: `08d4e7a` (terminology rename)

## Why This Matters

Clear terminology helps:
- New contributors understand the architecture faster
- Avoid confusion with other "shell" concepts
- Describe the pattern accurately (wrapping + delegating)
- Scale the mental model (wrapper/worker scales conceptually)
