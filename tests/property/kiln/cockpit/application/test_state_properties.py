from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from kiln.cockpit.application.state import (
    cache_share,
    extract_summary,
    format_age,
    total_token_usage,
)


@given(seconds=st.integers(min_value=-(10**6), max_value=10**7))
def test_formatted_age_is_never_negative(seconds: int) -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    assert not format_age(now - timedelta(seconds=seconds), now).startswith("-")


@given(text=st.text(max_size=500), limit=st.integers(min_value=1, max_value=200))
def test_extracted_summary_never_exceeds_its_limit(text: str, limit: int) -> None:
    assert len(extract_summary(text, limit)) <= limit


@given(
    values=st.dictionaries(st.text(min_size=1, max_size=10), st.integers(min_value=0), max_size=10)
)
def test_cache_share_is_a_fraction_when_usage_exists(values: dict[str, int]) -> None:
    share = cache_share(values)
    if sum(values.values()) == 0:
        assert share is None
    else:
        assert share is not None
        assert 0 <= share <= 1


@given(
    rows=st.lists(
        st.dictionaries(
            st.sampled_from(("input", "output", "cache_read")),
            st.integers(min_value=0),
            max_size=3,
        ),
        max_size=12,
    )
)
def test_token_totals_equal_column_sums(rows: list[dict[str, int]]) -> None:
    statuses = {str(index): {"token_usage": row} for index, row in enumerate(rows)}
    totals = total_token_usage(statuses)
    for kind in {key for row in rows for key in row}:
        assert totals[kind] == sum(row.get(kind, 0) for row in rows)
