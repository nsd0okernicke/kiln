from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.status_contract import (
    STATUS_BLOCKED,
    STATUS_DONE,
    is_valid_work_item_name,
    parse_worker_report,
)

summary = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_./",
    max_size=100,
)
valid_name = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}", fullmatch=True)


@given(status=st.sampled_from((STATUS_DONE, STATUS_BLOCKED)), detail=summary)
def test_valid_status_sentinel_round_trips(status: str, detail: str) -> None:
    result = parse_worker_report(f"noise\nKILN-STATUS: {status} {detail}")
    assert result.status == status
    assert result.summary == detail.strip()
    assert result.sentinel_found


@given(
    earlier=st.sampled_from((STATUS_DONE, STATUS_BLOCKED)),
    last=st.sampled_from((STATUS_DONE, STATUS_BLOCKED)),
)
def test_last_status_sentinel_wins(earlier: str, last: str) -> None:
    result = parse_worker_report(f"KILN-STATUS: {earlier} old\nKILN-STATUS: {last} final")
    assert result.status == last
    assert result.summary == "final"


@given(name=valid_name)
def test_generated_valid_work_item_names_are_accepted(name: str) -> None:
    assert is_valid_work_item_name(name)


@given(name=st.text(min_size=81))
def test_names_over_the_length_limit_are_rejected(name: str) -> None:
    assert not is_valid_work_item_name(name)
