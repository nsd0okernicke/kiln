"""
Per-backend one-shot worker invocation.

Each adapter splits into a pure `build_command(...)` (argv construction, unit-testable
without spawning anything) and a thin `run_worker(...)` that shells out. That boundary is
where constitution/engineering.md's "environmentally unsuitable" line falls: everything
above it is tested directly, and only the subprocess call itself needs a live backend.

`WorkerInvocation` lives here rather than inside any one adapter module: `claude_adapter.py`,
`copilot_adapter.py` and `codex_adapter.py` all return it, so it belongs to the package, not
to whichever adapter happened to be written first.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
from dataclasses import dataclass

from ..status_contract import WorkerResult

log = logging.getLogger("kiln-scheduler")

#: How long to wait for a killed tree to actually go away before giving up on it. The reader
#: loop is already unblocked by then -- this only bounds the tidy-up.
REAP_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class TokenUsage:
    """
    Token counts for one worker invocation, in the shape every backend can fill.

    A value type rather than four fields on `WorkerInvocation` because these numbers are
    summed twice — across retry attempts in `role_scheduler._Attempts`, and across roles in
    the dashboard — and that arithmetic belongs in one place rather than being restated at
    each call site.

    Cache fields stay separate from `input_tokens` rather than being folded into it: a
    cache read is charged differently from a fresh input token, so a total that silently
    merged them would misrepresent exactly the thing this exists to measure. Backends that
    report no cache breakdown simply leave them at zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(frozen=True)
class WorkerInvocation:
    """Outcome of one worker process, including why it failed when it did."""

    result: WorkerResult
    raw_output: str
    cost_usd: float = 0.0
    is_error: bool = False
    timed_out: bool = False
    detail: str = ""
    #: None means the backend reported no usage at all, which is NOT the same as zero
    #: tokens. Same rule `set-status.py::build_status` already applies to `cycles`/`cost_usd`
    #: — a surface that never measured something must not claim it measured zero. A blocked
    #: or crashed invocation legitimately has nothing to report.
    tokens: TokenUsage | None = None

    @property
    def is_done(self) -> bool:
        return self.result.is_done


def terminate_tree(process: subprocess.Popen) -> None:
    """
    Kill the worker *and everything it spawned*.

    `Popen.kill()` reaches only the direct child. Every worker shells out — codex runs
    `pwsh -Command 'uv run pytest ...'` — and the grandchildren inherit the stdout pipe, so
    killing the child alone leaves the reader loop blocked on a pipe that still has writers.
    The watchdog fires, and the worker keeps running anyway.

    Observed live: an 1800s cap released at 2698s (898s over) because a `testcontainers`
    fixture was still waiting on a Docker daemon that was not running. The first attempt of
    the same handoff released on time, so the overrun is unbounded rather than constant —
    it is however long the deepest surviving descendant takes to give up.

    Best effort by design: a process that is already gone, or that we may not signal, is not
    an error worth failing a cycle over. The caller has a timeout either way.
    """
    if process.poll() is not None:
        return

    if os.name == "nt":
        # /T covers the descendant chain; /F because a hung child will not honour a request.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True, check=False,
        )
    else:
        _killpg(process)

    # Belt and braces: taskkill can miss a process started under a different account, and
    # `_killpg` declines outright when the group looks like our own.
    with contextlib.suppress(OSError):
        process.kill()
    try:
        process.wait(timeout=REAP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        log.warning("worker pid %s outlived its kill; leaving it to the OS", process.pid)


def _killpg(process: subprocess.Popen) -> None:
    """
    Signal the child's whole process group, but never our own.

    This is only safe because the adapters spawn with `start_new_session=True`, which makes
    the child a group leader. Without it `getpgid(child)` returns *the scheduler's* group and
    the kill takes down the scheduler with the worker — so the guard below is load-bearing,
    not defensive padding.
    """
    try:
        group = os.getpgid(process.pid)
    except OSError:
        # Gone, not ours, or not a pid at all — all of which mean there is nothing to signal.
        return
    if group == os.getpgrp():
        log.warning("worker pid %s shares our process group; not signalling it", process.pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)


__all__ = ["REAP_TIMEOUT_SEC", "TokenUsage", "WorkerInvocation", "terminate_tree"]
