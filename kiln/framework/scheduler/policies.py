"""Pure scheduler decisions, isolated from SQLite, Git, and worker processes."""

from __future__ import annotations

from collections.abc import Sequence

from .adapters import WorkerInvocation


def should_retry(invocations: Sequence[WorkerInvocation], max_attempts: int) -> bool:
    return bool(invocations and not invocations[-1].is_done and len(invocations) < max_attempts)


def cycle_limit_breach(
    *, arrivals: int, max_cycles: int | None, work_item: str | None, role: str
) -> str:
    if max_cycles is None or not work_item or arrivals <= max_cycles:
        return ""
    return (
        f"work item {work_item!r} has reached {role} {arrivals} times, over the "
        f"limit of {max_cycles}; stopping instead of running another cycle"
    )


def budget_breach(*, spent: float, maximum: float | None, work_item: str | None) -> str:
    if maximum is None or spent < maximum:
        return ""
    return (
        f"work item {work_item or '(unnamed)'!r} has cost ${spent:.2f} at this role, "
        f"at or over the ${maximum:.2f} cap; stopping instead of spending more"
    )


def escalation_halts(consecutive_escalations: int, limit: int) -> bool:
    return consecutive_escalations >= limit
