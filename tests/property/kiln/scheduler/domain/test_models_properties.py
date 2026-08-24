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


@given(left=usage, right=usage)
def test_token_usage_addition_sums_each_billing_category(
    left: TokenUsage, right: TokenUsage
) -> None:
    combined = left + right
    assert combined.input_tokens == left.input_tokens + right.input_tokens
    assert combined.output_tokens == left.output_tokens + right.output_tokens
    assert combined.cache_read_tokens == left.cache_read_tokens + right.cache_read_tokens
    assert combined.cache_creation_tokens == (
        left.cache_creation_tokens + right.cache_creation_tokens
    )


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
