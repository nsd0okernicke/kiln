"""Generated checks for the proxy capture domain's security and accounting invariants."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from kiln.proxy.domain.capture import (
    REDACTED,
    SENSITIVE_HEADERS,
    TRUNCATION_MARKER,
    CaptureMode,
    capture_body,
    extract_composition,
    extract_usage,
    redact_headers,
)

header_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu")) | st.just("-"),
    min_size=1,
    max_size=40,
)
headers = st.dictionaries(header_names, st.text(max_size=100), max_size=20)

json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=80)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=20), children, max_size=5)
    ),
    max_leaves=15,
)


@given(headers)
def test_header_redaction_is_complete_and_idempotent(original: dict[str, str]) -> None:
    redacted = redact_headers(original)

    assert redacted.keys() == original.keys()
    assert redact_headers(redacted) == redacted
    for name, value in original.items():
        expected = REDACTED if name.lower() in SENSITIVE_HEADERS else value
        assert redacted[name] == expected


@given(body=st.text(max_size=500), limit=st.integers(min_value=0, max_value=200))
def test_body_capture_obeys_mode_and_limit(body: str, limit: int) -> None:
    assert capture_body(body, CaptureMode.METADATA, limit) is None

    captured = capture_body(body, CaptureMode.FULL, limit)
    if len(body) <= limit:
        assert captured == body
    else:
        assert captured == body[:limit] + TRUNCATION_MARKER


@given(tools=json_values, system=json_values, messages=json_values)
def test_composition_matches_compact_utf8_json(
    tools: object, system: object, messages: object
) -> None:
    payload = {"tools": tools, "system": system, "messages": messages}
    body = json.dumps(payload, ensure_ascii=False)

    assert extract_composition(body) == {
        name: len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        for name, value in payload.items()
    }


@given(
    fresh=st.integers(min_value=0, max_value=10**9),
    cached=st.integers(min_value=0, max_value=10**9),
    written=st.integers(min_value=0, max_value=10**9),
    output=st.integers(min_value=0, max_value=10**9),
)
def test_responses_usage_preserves_token_totals(
    fresh: int, cached: int, written: int, output: int
) -> None:
    total = fresh + cached + written
    body = json.dumps(
        {
            "usage": {
                "input_tokens": total,
                "output_tokens": output,
                "input_tokens_details": {
                    "cached_tokens": cached,
                    "cache_write_tokens": written,
                },
            }
        }
    )

    usage = extract_usage(body)

    assert usage is not None
    assert usage.input_tokens == fresh
    assert usage.cache_read_tokens == cached
    assert usage.cache_creation_tokens == written
    assert usage.output_tokens == output
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens == total
