"""Scheduler application state and results, independent of concrete infrastructure."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import policies
from .adapters import TokenUsage, WorkerInvocation
from .ports import MessageQueue, VerificationResult, WorkerRunner, Worktree
from .routing import RoutingTable
from .worker_prompt import WorkerDefinition

# Cycle outcomes are application results, not CLI or persistence concerns.
IDLE = "idle"
HANDED_OFF = "handed_off"
PING_FORWARDED = "ping_forwarded"
ESCALATED = "escalated"
MERGE_FAILED = "merge_failed"
NO_ROUTE = "no_route"
HALTED = "halted"
NO_OP = "no_op"
MAX_CYCLES = "max_cycles"
COST_CAP = "cost_cap"


@dataclass
class SchedulerContext:
    """Dependencies and configuration required by one scheduler application instance."""

    role: str
    branch: str
    db_path: Path
    worktree: Path
    routing: RoutingTable
    definition: WorkerDefinition
    worker_runner: WorkerRunner
    queue: MessageQueue
    worktree_port: Worktree
    clock: Callable[[], datetime] = datetime.now
    set_status: Callable[..., None] = lambda _state, **_kwargs: None
    max_attempts: int = 2
    escalation_limit: int = 3
    max_cycles: int | None = None
    max_budget_usd: float | None = None
    run_verify: Callable[[], VerificationResult] | None = None

    def timestamp(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SchedulerState:
    """Mutable application state that survives across cycles in one process."""

    consecutive_escalations: int = 0
    halted: bool = False
    parked: bool = False
    spend_by_work_item: dict[str, float] = field(default_factory=dict)

    def spend_on(self, work_item: str | None) -> float:
        return self.spend_by_work_item.get(work_item or "", 0.0)

    def record_spend(self, work_item: str | None, cost: float) -> None:
        key = work_item or ""
        self.spend_by_work_item[key] = self.spend_by_work_item.get(key, 0.0) + cost


@dataclass(frozen=True)
class CycleResult:
    outcome: str
    message_id: str | None = None
    target: str | None = None
    detail: str = ""
    cost_usd: float = 0.0
    attempts: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class Attempts:
    """Worker attempts for one message, newest last."""

    invocations: list[WorkerInvocation] = field(default_factory=list)

    @property
    def last(self) -> WorkerInvocation:
        return self.invocations[-1]

    @property
    def cost(self) -> float:
        return sum(inv.cost_usd for inv in self.invocations)

    @property
    def tokens(self) -> TokenUsage:
        total = TokenUsage()
        for invocation in self.invocations:
            if invocation.tokens is not None:
                total = total + invocation.tokens
        return total


def should_retry(invocations: Sequence[WorkerInvocation], max_attempts: int) -> bool:
    """Retry only while the latest attempt failed and the attempt allowance remains."""
    return policies.should_retry(invocations, max_attempts)
