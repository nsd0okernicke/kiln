# Live Pane Plan — showing scheduler and worker output in the cockpit

## Problem and approach

The web cockpit (issue #22) answers *what* the swarm is doing — which role holds which work
item, what it has spent, what needs attention — but not *what is happening right now inside a
role*. That narration only exists in the WezTerm pane, which means an operator watching the
browser still has to switch to a terminal tab to see why a role has been busy for four
minutes, and loses it entirely once the window closes.

Two separate streams make up "what the agent is doing", and they are in very different states
today. That difference is what drives the phasing below: one is already on disk and needs
only an endpoint, the other is not persisted anywhere and needs a small scheduler change
first.

### Stream 1 — the scheduler's own narration (already on disk)

Every scheduler pane already writes `.kiln/logs/scheduler-<role>.log` through
`role_scheduler.configure_logging`, which installs a `FileHandler` alongside the pane's
`StreamHandler`. The file is complete, timestamped and structured. From a live run:

```
22:26:04 [kiln-scheduler/INFO] 📥 received handoff 0d980051 from human-in-the-loop (name=pending)
22:26:04 [kiln-scheduler/INFO] 🔀 merging run1 from run1
22:26:04 [kiln-scheduler/INFO] 🤖 delegating to specifier-worker (attempt 1/2)
22:27:28 [kiln-scheduler/INFO] worker specifier-worker finished: status=done sentinel=True cost=$0.1786 tokens=372305
22:27:28 [kiln-scheduler/INFO] ✅ worker done: Verified CAT-3 Gherkin spec … ready to hand off to coder.
22:27:28 [kiln-scheduler/INFO] 📤 work item named: CAT-3
```

This costs no scheduler change at all. The cockpit only has to read the file.

### Stream 2 — the worker's streamed output (not persisted)

Note the gap above: `delegating` at 22:26:04, `worker finished` at 22:27:28 — **84 seconds of
silence** covering the part an operator most wants to watch. The worker's rendered output
goes straight to the pane and is never written anywhere:

```python
def _default_emit(line: str) -> None:
    """Worker output goes straight to the pane, unbuffered, so progress is visible live."""
    print(line, flush=True)
```

So a cockpit that only tails the scheduler log shows crisp cycle boundaries and then goes
quiet for exactly the interesting minute.

The seam to fix that already exists and is small: `role_scheduler._make_worker_output_emitter()`
is one isolated function, and every adapter (`claude`, `codex`, `copilot`, `grok`) already
honours `run_worker(on_output=…)` — `emit = on_output or _default_emit`. Nothing needs a new
parameter.

## Todos

### Phase A — tail the scheduler log (no scheduler change)

1. **New endpoint `GET /api/logs/<role>?after=<offset>`** in `cockpit/server.py`
   - Return `{"role", "offset", "lines": [...], "truncated": bool}` — new bytes since
     `after`, plus the new offset for the next poll. A byte offset rather than a line count:
     the file only ever grows, so an offset makes the follow-up read O(new bytes) instead of
     re-reading and re-splitting the whole file every two seconds.
   - **Validate `role` against the sessions file first**, exactly as `_send_status` does. The
     path is built from the URL and this server has no authentication; without the check,
     `..` is a file-read primitive on the operator's machine.
   - Cap the first read (last ~64 KB, `truncated: true` when clipped) so a long run cannot
     return a multi-megabyte first response.
   - Handle rotation/truncation: when the file is shorter than `after`, reset to 0 and say so
     rather than returning garbage.
   - A missing log file is `200` with no lines, not `404` — a role that has not started yet
     is a normal state, not an error.

2. **Log panel in `static/cockpit.html`**
   - Clicking a Work Queue row expands a `<pre>` panel beneath it, polled on the existing 2s
     tick. Only the expanded role is polled, so a collapsed board costs nothing extra.
   - Keep the panel pinned to the bottom unless the operator has scrolled up — the same rule
     any log viewer needs, and the reason not to simply re-render the whole panel each tick.
   - Reuse the existing `--code-bg` token so all three themes are covered for free.

3. **Tests** in `tests/test_cockpit_server.py`
   - New bytes only are returned for a non-zero `after`; the offset advances.
   - A role not in the sessions file is refused; a traversal attempt in the role name is
     refused (mirroring the existing `/api/status/<role>` cases).
   - A truncated/rotated file resets rather than returning nonsense.
   - A missing log file answers `200` with no lines.

### Phase B — capture the worker stream (small scheduler change)

4. **Tee worker output to a file** in `role_scheduler._make_worker_output_emitter()`
   - Keep printing to the pane exactly as now, including the existing tint and the
     `sys.stdout.isatty()` rule that keeps escape sequences out of piped output. Add an
     append to `.kiln/logs/worker-<role>.log`.
   - The pane must stay the authority on liveness: a failed write to the log is logged once
     and then ignored, never raised. The view must not be able to break the run.
   - Size cap with rotation (see open question below).

5. **Point the panel at both files**
   - The panel shows the scheduler log by default and offers the worker log alongside it, or
     interleaves them by timestamp. Interleaving is the better experience and the more
     fragile implementation; decide once Phase A is in use.

6. **Tests**
   - The emitter still prints every line to stdout unchanged (the existing behaviour is what
     the pane depends on).
   - Lines also land in the worker log.
   - An unwritable log directory does not raise and does not stop the worker.

## Open questions

- **Rotation policy for `worker-<role>.log`.** A chatty run writes a lot. A simple size cap
  with one rollover file is probably enough; a `RotatingFileHandler` is the obvious tool but
  the emitter is not a `logging` call today, so it would either become one or grow a small
  cap of its own.
- **On by default, or behind `workerDebug`?** Recommendation: **on by default with a size
  cap.** The pane scrollback dies with the window, and that is precisely the loss
  `configure_logging`'s file handler was added to prevent — the same argument applies to the
  worker's half of the narration. `workerDebug` remains what it is now: the backend CLI's own
  internal trace, which is a different and much larger artefact.
- **Interleaved or separate panes** for the two logs (see Todo 5).

## Non-goals

- **No SSE or WebSocket.** Polling a byte offset is strictly simpler, and the terminal
  dashboard has run on a 2-second poll since it shipped. Revisit only if polling proves
  visibly laggy in real use — the same bar issue #22 set for the rest of the cockpit.
- **No pane capture.** Reading the actual terminal pane's scrollback (via `wezterm cli` or
  tmux) was already ruled out in issue #22 and stays ruled out: it is backend-specific,
  scrapes a rendered view rather than a source of truth, and both streams above are available
  without it.
- **No log search or filtering** in v1. Tail and scroll first; see whether anything more is
  actually wanted.

## Verification

1. `python -m pytest -p no:cacheprovider` and `python -m ruff check src tests`.
2. Against a scratch project: write a known line into `.kiln/logs/scheduler-coder.log`, poll
   `GET /api/logs/coder`, append another line, and confirm the second poll returns only the
   new one.
3. Confirm the refusals directly — `GET /api/logs/attacker` and a traversal in the role name
   both `404`.
4. On a live run: expand the specifier row while it is delegating and confirm the panel keeps
   up with the pane. After Phase B, confirm the 84-second silence between `delegating` and
   `worker finished` is filled with the worker's own narration.
