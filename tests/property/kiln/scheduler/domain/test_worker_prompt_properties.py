from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.worker_prompt import parse_tools_list

tool = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,20}", fullmatch=True)


@given(tools=st.lists(tool, max_size=20), padding=st.integers(min_value=0, max_value=4))
def test_tool_lists_round_trip_comma_separated_names(tools: list[str], padding: int) -> None:
    spaces = " " * padding
    encoded = ",".join(f"{spaces}{name}{spaces}" for name in tools)
    assert parse_tools_list(encoded) == tools
