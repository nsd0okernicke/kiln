from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.application.process_next_message import (
    is_pending,
    resolve_work_item,
    work_item_of,
)


@given(
    spaces=st.text(alphabet=" \t", max_size=10),
    casing=st.sampled_from(("pending", "PENDING", "Pending")),
)
def test_pending_is_case_and_whitespace_insensitive(spaces: str, casing: str) -> None:
    value = f"{spaces}{casing}{spaces}"
    assert is_pending(value)
    assert work_item_of(value) is None


@given(inbound=st.text(max_size=80), reported=st.text(max_size=80))
def test_only_pending_work_items_can_be_renamed(inbound: str, reported: str) -> None:
    resolved = resolve_work_item(inbound, reported)
    if is_pending(inbound):
        assert resolved == (reported or inbound)
    else:
        assert resolved == inbound
