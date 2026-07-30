<!-- Raw prose drafted alongside coder-internal-cycle.mmd, kept for reuse in README.md and the slide
deck. Not wired into either yet — copy/trim what's needed when the time comes. -->

# Coder Internal Cycle — reusable prose

## Title / framing

**Eyebrow:** Kiln — agent internals
**Title:** What the Coder wrapper does with a message

**Lede:**
> One loop cycle, drawn as two nested loops: the persistent wrapper that owns the message queue and
> git, and the disposable coder-worker subagent it dispatches to do the actual TDD work. Every
> other role (specifier, refactorer, architect) follows the same wrapper shape — only the worker's
> inner loop changes.

## Legend

- **Wrapper** — persistent, thin CLAUDE.md, owns git/queue
- **Worker** — disposable subagent, does the implementation

## Short caption (used under the simplified diagram)

> The wrapper (blue) never touches code — it waits, merges the sender's commit, hands the work to a
> fresh worker subagent, and sends the squashed result onward. The worker (orange) is the only part
> that's role-specific: swap its inner loop for coverage → CRAP → mutation and it's the refactorer
> instead.

## Fuller step-by-step (from the first draft — good detail for README/slides, too dense for the diagram page itself)

### Wrapper — six steps, every cycle

Persistent process. Owns the git worktree, the message queue, and status signaling. Never
implements anything itself.

1. **Receive.** `wait_for_message()` blocks until a row arrives, then `/kiln-receive` persists it,
   **merges the sender's commit** into the wrapper's branch, and logs it.
2. **Mark processing.** Flips the message row from `delivered` to `processing`.
3. **Delegate.** Dispatches `coder-worker` via the Agent tool and blocks — the wrapper does not touch
   code itself.
4. **Handle failure.** One retry with the failure fed back in; a second failure proceeds anyway so
   the loop can't stall.
5. **Handoff.** `/kiln-handoff` squashes since the last merge commit and inserts the outbound
   message, retrying until a row is confirmed.
6. **Mark processed,** then loop straight back to step 1 in the same turn.

### Worker — role-specific inner loop

Disposable subagent, spun up fresh per cycle with the full handoff content in its prompt. This is
the part that differs per role — the coder's is the TDD cycle shown here.

- **red / green / refactor** — Runs `/tdd-red` → `/tdd-green` → `/tdd-refactor` per behavior slice,
  repeating until every slice from the handoff is covered.
- **verify** — Local test/lint/type-check pass before reporting — never handed back untested.
- **report** — Returns only a final summary to the wrapper: what was implemented, what was verified,
  which files changed. No intermediate chatter crosses back.

## Footer / source note

> Source at `docs/diagrams/coder-internal-cycle.mmd`.

## Notes for reuse

- The "swap the worker's inner loop and it's another role" framing (refactorer's inner loop would be
  coverage → CRAP → mutation gates) is worth keeping as the one-sentence explanation of why there's
  only one diagram instead of four.
- README: probably wants the short caption + legend, not the six-step breakdown (too granular for a
  top-level doc).
- Slide deck: the fuller step-by-step is closer to spoken-talk-track material — one slide per wrapper
  step, one slide for the worker loop.
- Terminology: as of 2026-07-29, "shell" was renamed to "wrapper" project-wide (README, TODO,
  technical-architecture-slides.md, docs/diagrams/wrapper-worker.mmd, the `kiln.ps1`/`kiln.sh`
  worker-agent generators, and the generated `.github/agents/*-worker.agent.md` descriptions) — the
  term was judged misleading since it has nothing to do with a command-line shell. This file already
  reflects that; if pulling from an older copy, check for stray "shell" wording.
