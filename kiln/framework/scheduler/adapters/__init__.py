"""
Per-backend one-shot worker invocation.

Each adapter splits into a pure `build_command(...)` (argv construction, unit-testable
without spawning anything) and a thin `run_worker(...)` that shells out. That boundary is
where constitution/engineering.md's "environmentally unsuitable" line falls: everything
above it is tested directly, and only the subprocess call itself needs a live backend.
"""

from .claude_adapter import WorkerInvocation

__all__ = ["WorkerInvocation"]
