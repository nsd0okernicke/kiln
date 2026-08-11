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

from dataclasses import dataclass

from ..status_contract import WorkerResult


@dataclass(frozen=True)
class WorkerInvocation:
    """Outcome of one worker process, including why it failed when it did."""

    result: WorkerResult
    raw_output: str
    cost_usd: float = 0.0
    is_error: bool = False
    timed_out: bool = False
    detail: str = ""

    @property
    def is_done(self) -> bool:
        return self.result.is_done


__all__ = ["WorkerInvocation"]
