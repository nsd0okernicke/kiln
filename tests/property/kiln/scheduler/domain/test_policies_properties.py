from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.models import WorkerInvocation
from kiln.scheduler.domain.policies import (
    budget_breach,
    cycle_limit_breach,
    escalation_halts,
    should_retry,
)
from kiln.scheduler.domain.status_contract import STATUS_BLOCKED, STATUS_DONE, WorkerResult


@given(
    attempt_count=st.integers(min_value=0, max_value=10),
    max_attempts=st.integers(min_value=0, max_value=10),
    last_done=st.booleans(),
)
def test_retry_requires_a_failed_last_attempt_and_remaining_capacity(
    attempt_count: int, max_attempts: int, last_done: bool
) -> None:
    result = WorkerResult(
        status=STATUS_DONE if last_done else STATUS_BLOCKED,
        summary="",
        sentinel_found=True,
    )
    invocations = [WorkerInvocation(result=result, raw_output="") for _ in range(attempt_count)]

    assert should_retry(invocations, max_attempts) is (
        attempt_count > 0 and not last_done and attempt_count < max_attempts
    )


@given(arrivals=st.integers(min_value=0), limit=st.integers(min_value=0))
def test_cycle_limit_breaches_exactly_above_the_limit(arrivals: int, limit: int) -> None:
    breach = cycle_limit_breach(arrivals=arrivals, max_cycles=limit, work_item="work", role="r")
    assert bool(breach) is (arrivals > limit)


@given(
    spent=st.floats(min_value=0, max_value=10**9, allow_nan=False),
    maximum=st.floats(min_value=0, max_value=10**9, allow_nan=False),
)
def test_budget_breaches_at_or_above_the_cap(spent: float, maximum: float) -> None:
    breach = budget_breach(spent=spent, maximum=maximum, work_item="work")
    assert bool(breach) is (spent >= maximum)


@given(count=st.integers(), limit=st.integers())
def test_escalation_threshold_is_inclusive(count: int, limit: int) -> None:
    assert escalation_halts(count, limit) is (count >= limit)
