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


__all__ = ["TokenUsage", "WorkerInvocation"]
