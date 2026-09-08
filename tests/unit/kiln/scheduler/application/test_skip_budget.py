"""The skip-budget guard: does a repeated GATE_SKIP actually block a handoff?

`skip_record.py` is well covered on its own, but the wiring that reads a worker's output,
combines it with the work item's history and rewrites the attempt as blocked had no test. Across
five swarm runs no role has ever skipped a gate, so the live path has never executed once -- it
is the most carefully built rule in the scheduler and the only one with no evidence behind it.
These tests are that evidence: they exercise `_check_skip_budget` directly, so the chain is
proven even though no run has yet triggered it.
"""

from __future__ import annotations

import pytest

from kiln.scheduler.application import process_next_message as scheduler
from kiln.scheduler.domain import skip_record, status_contract
from kiln.scheduler.domain.models import WorkerInvocation


def _result(summary: str, work_item: str = "cat-1-search") -> status_contract.WorkerResult:
    return status_contract.WorkerResult(
        status=status_contract.STATUS_DONE,
        summary=summary,
        sentinel_found=True,
        handoff_name=work_item,
    )


def _attempts(summary: str, work_item: str = "cat-1-search") -> scheduler.Attempts:
    invocation = WorkerInvocation(result=_result(summary, work_item), raw_output=summary)
    return scheduler.Attempts(invocations=[invocation])


class _Queue:
    """Only the one method the guard calls; anything else is not part of this contract."""

    def __init__(self, history: list[dict] | None = None) -> None:
        self.history = history or []
        self.asked_for: list[str] = []

    def messages_for_work_item(self, work_item: str, limit: int = 20) -> list[dict]:
        self.asked_for.append(work_item)
        return self.history


class _ExplodingQueue(_Queue):
    def messages_for_work_item(self, work_item: str, limit: int = 20) -> list[dict]:
        raise RuntimeError("queue unavailable")


def _make_ctx(queue: _Queue) -> scheduler.SchedulerContext:
    """A context carrying only the two attributes the guard touches."""
    ctx = object.__new__(scheduler.SchedulerContext)
    object.__setattr__(ctx, "queue", queue)
    object.__setattr__(ctx, "role", "coder")
    return ctx


SKIP = "GATE_SKIP: gate=acceptance reason=container_unavailable detail=postgres failed"
OTHER = "GATE_SKIP: gate=mutation reason=no_mutation_targets detail=infra only"


def _history(*summaries: str) -> list[dict]:
    return [{"content": s} for s in summaries]


class TestSkipBudget:
    def test_output_without_a_skip_record_is_left_alone(self):
        attempts = _attempts("Implemented CAT-1; all gates green")
        scheduler._check_skip_budget(_make_ctx(_Queue()), attempts)
        assert attempts.last.result.status == status_contract.STATUS_DONE

    def test_a_single_skip_is_allowed(self):
        """One skip is the escape hatch working as intended, not a budget breach."""
        attempts = _attempts(f"Implemented CAT-1\n{SKIP}")
        scheduler._check_skip_budget(_make_ctx(_Queue()), attempts)
        assert attempts.last.result.status == status_contract.STATUS_DONE

    def test_the_same_gate_and_reason_repeated_past_the_budget_blocks_the_handoff(self):
        """The rule the whole mechanism exists for: a habit, not a one-off, is refused."""
        queue = _Queue(_history(f"earlier cycle\n{SKIP}", f"earlier cycle\n{SKIP}"))
        attempts = _attempts(f"Implemented CAT-1\n{SKIP}")

        scheduler._check_skip_budget(_make_ctx(queue), attempts)

        assert attempts.last.result.status == status_contract.STATUS_BLOCKED
        assert "acceptance:container_unavailable" in attempts.last.result.summary
        assert queue.asked_for == ["cat-1-search"]

    def test_different_gates_do_not_pool_against_one_budget(self):
        """Skipping two different gates once each is two escape hatches, not a repeat."""
        queue = _Queue(_history(f"earlier\n{OTHER}", f"earlier\n{OTHER}"))
        attempts = _attempts(f"Implemented CAT-1\n{SKIP}")

        scheduler._check_skip_budget(_make_ctx(queue), attempts)

        assert attempts.last.result.status == status_contract.STATUS_DONE

    def test_history_is_scoped_to_this_work_item(self):
        """A gate skipped repeatedly on another story must not block this one."""
        queue = _Queue()
        attempts = _attempts(f"done\n{SKIP}", work_item="loan-4-return")
        scheduler._check_skip_budget(_make_ctx(queue), attempts)
        assert queue.asked_for == ["loan-4-return"]

    def test_an_unavailable_queue_does_not_take_the_cycle_down(self):
        """A guard that cannot read history degrades to the current attempt, never raises."""
        attempts = _attempts(f"Implemented CAT-1\n{SKIP}")
        scheduler._check_skip_budget(_make_ctx(_ExplodingQueue()), attempts)
        assert attempts.last.result.status == status_contract.STATUS_DONE

    def test_blocking_preserves_whether_the_sentinel_was_seen(self):
        """'worker reported blocked' and 'worker never reported' stay distinguishable."""
        queue = _Queue(_history(SKIP, SKIP))
        attempts = _attempts(f"done\n{SKIP}")
        scheduler._check_skip_budget(_make_ctx(queue), attempts)
        assert attempts.last.result.sentinel_found is True

    @pytest.mark.parametrize("budget", [skip_record.DEFAULT_SKIP_BUDGET])
    def test_the_budget_is_the_documented_one(self, budget):
        """The blocking threshold is the domain constant, not a number hidden in the guard."""
        queue = _Queue(_history(*[SKIP] * budget))
        attempts = _attempts(f"done\n{SKIP}")
        scheduler._check_skip_budget(_make_ctx(queue), attempts)
        assert attempts.last.result.status == status_contract.STATUS_BLOCKED
