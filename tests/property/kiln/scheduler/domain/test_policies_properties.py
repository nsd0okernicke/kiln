from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.policies import budget_breach, cycle_limit_breach, escalation_halts


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
