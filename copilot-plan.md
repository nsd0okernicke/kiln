# Issue #8 Implementation Plan

## Problem and approach

Copilot scheduler workers run as non-interactive one-shot processes through
`kiln/framework/scheduler/adapters/copilot_adapter.py`. On affected CLI versions, a long-running
session can enter an unrecoverable state where write-capable tool calls fail with
`Permission denied and could not request permission from user`. The current repository already
trusts Copilot worktrees, filters wrapper-only skills, grants explicit tool permissions, and can
capture backend debug logs, but Copilot remains removed from every shipped scheduler profile.

The upstream report was self-closed without a documented resolution. The local CLI is 1.0.80,
and a newer related upstream report identifies 1.0.80 as a working control, but that is not enough
to treat the bug as fixed. Implement a Kiln-side recovery that recognizes the exact poisoned-session
failure, terminates the process immediately, and returns a blocked invocation. The existing bounded
scheduler retry then starts a fresh Copilot process with the failure summary and preserves any
partial work already present in the worktree.

## Todos

1. **Classify the fatal Copilot permission failure**
   - Add a narrow helper in `kiln/framework/scheduler/adapters/copilot_adapter.py` that recognizes
     the exact `Permission denied and could not request permission from user` text in failed
     `tool.execution_complete` events.
   - Keep ordinary tool failures and unrelated permission errors on the existing path.
   - Produce an explicit summary identifying an unrecoverable Copilot session permission state,
     rather than allowing the session to continue spending credits or later reporting a generic
     missing final reply.

2. **Abort poisoned sessions and reuse scheduler retries**
   - In `run_worker()`, capture the triggering event, terminate the Copilot process tree, and return
     a blocked/error `WorkerInvocation` after the stream closes.
   - Preserve captured JSONL output and any available token usage for post-mortem/debug accounting.
   - Do not add a second retry mechanism inside the adapter; rely on
     `role_scheduler._delegate()` and `maxAttempts` so retry counting, status, escalation, and debug
     persistence remain centralized.
   - Preserve partial work in the worktree. The fresh retry should inspect and continue from that
     state, while the existing verification and squash stages remain the final correctness gates.

3. **Add focused regression coverage**
   - Extend `tests/test_copilot_adapter.py` for exact failure classification, non-matching failures,
     immediate process-tree termination, blocked result details, raw-output preservation, and token
     preservation where usage is present before the abort.
   - Add or adjust scheduler coverage only where needed to prove the adapter-produced blocked result
     receives a fresh second invocation and escalates after the configured attempt limit.
   - Keep existing command flags, worktree trust, skill filtering, debug logging, and generic timeout
     behavior covered and unchanged.

4. **Run a controlled live acceptance test**
   - Use a fresh disposable Kiln project/worktree with `workerDebug: true` and Copilot CLI 1.0.80 or
     newer.
   - Exercise a write-heavy scheduler task beyond the historical 4-8 minute failure window and
     confirm either uninterrupted completion or fast termination followed by a successful fresh
     retry.
   - Confirm a poisoned session cannot continue consuming the full worker timeout, partial changes
     survive retry, successful work passes verification, and the scheduler produces a normal
     handoff.

5. **Conditionally restore Copilot to the mixed-backends fixture**
   - If the live acceptance test passes, change the `specifier` role in
     `kiln/framework/profiles.json` back to `agent: copilot`, retaining `workerDebug: true` during the
     initial restored fixture.
   - Update the profile description and README profile example so shipped configuration and
     documentation agree.
   - If the acceptance test fails across both configured attempts, leave Copilot parked and document
     the captured failure; do not mask it with a profile change.

6. **Update issue-facing documentation**
   - Revise README's Known Limitations entry to distinguish the unresolved upstream root cause from
     Kiln's bounded fail-fast recovery.
   - Document that repeated failure still escalates to a human through the standard scheduler policy.
   - Record ConPTY/PTY invocation as a deferred fallback only if fresh-process recovery proves
     ineffective; avoid adding `pywinpty` or a Windows-only process path without evidence it solves
     the failure.

7. **Validate the implementation**
   - Run the targeted Copilot adapter and scheduler tests.
   - Run Ruff on changed Python files and the documentation consistency tests.
   - Run the full test suite after the targeted checks pass because the adapter, profile, and README
     are shared framework surfaces.

## Notes and considerations

- The exact error string is intentionally the recovery trigger. Broadly treating every permission
  error as a poisoned session could terminate legitimate, actionable failures.
- A fresh-process retry is supported by the original investigation: short sessions in the same
  worktree succeeded immediately before and after failed long sessions.
- The existing scheduler already owns bounded retries, escalation, debug artifacts, verification,
  and partial worktree state. Reusing it is lower risk than adapter-local retries or a new PTY
  subprocess abstraction.
- No minimum Copilot version guard should be introduced until a release is officially confirmed to
  fix the root cause; current upstream evidence is observational rather than contractual.
