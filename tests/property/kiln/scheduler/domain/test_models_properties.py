from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.models import TokenUsage

counts = st.integers(min_value=0, max_value=10**9)
usage = st.builds(TokenUsage, counts, counts, counts, counts)


@given(left=usage, right=usage, third=usage)
def test_token_usage_addition_is_a_commutative_monoid(
    left: TokenUsage, right: TokenUsage, third: TokenUsage
) -> None:
    zero = TokenUsage()
    assert left + zero == left
    assert zero + left == left
    assert left + right == right + left
    assert (left + right) + third == left + (right + third)


@given(value=usage)
def test_token_total_is_the_sum_of_all_billing_categories(value: TokenUsage) -> None:
    assert value.total == sum(
        (
            value.input_tokens,
            value.output_tokens,
            value.cache_read_tokens,
            value.cache_creation_tokens,
        )
    )
