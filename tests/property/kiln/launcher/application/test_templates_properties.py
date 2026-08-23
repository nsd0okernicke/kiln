from hypothesis import given
from hypothesis import strategies as st

from kiln.launcher.application.templates import BLOCK_SEPARATOR, apply_substitutions, join_blocks


@given(blocks=st.lists(st.text(max_size=100), max_size=10))
def test_join_blocks_keeps_exactly_the_nonblank_blocks(blocks: list[str]) -> None:
    expected = [block for block in blocks if block and block.strip()]
    assert join_blocks(blocks) == BLOCK_SEPARATOR.join(expected)


@given(text=st.text(max_size=300), value=st.text(max_size=100))
def test_literal_substitution_removes_the_placeholder(text: str, value: str) -> None:
    placeholder = "{{KILN_PROPERTY_PLACEHOLDER}}"
    result = apply_substitutions(text + placeholder, {placeholder: value})
    assert result == text.replace(placeholder, value) + value
