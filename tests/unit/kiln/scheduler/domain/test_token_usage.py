"""
Token accounting across the four adapters — issue #6 Phase A.

Every backend already reports usage in the stream its adapter was parsing anyway; these
tests pin what each one extracts and, just as importantly, what it does when it finds
nothing. The `None` cases matter more than the happy paths here: the whole point of this
feature is measurement, and a confidently wrong number is worse than a missing one.

The **Claude and Grok** shapes are the Anthropic Messages API wire format, whose key names
are pinned by a public API. The **Codex and Copilot** shapes are taken from those adapters'
own module docstrings and have NOT been checked against a captured stream — see each
adapter's `_USAGE_ALIASES` note. The `..._reports_nothing_when_the_shape_is_unfamiliar`
tests are what make a wrong guess safe.
"""

from __future__ import annotations

import json

from kiln.scheduler.infrastructure.agents import (
    TokenUsage,
    claude_adapter,
    codex_adapter,
    copilot_adapter,
    grok_adapter,
)


def _stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestTokenUsage:
    def test_total_sums_every_field(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_creation_tokens=40
        )
        assert usage.total == 100

    def test_addition_is_field_wise(self):
        combined = TokenUsage(input_tokens=1, output_tokens=2) + TokenUsage(
            input_tokens=10, cache_read_tokens=5
        )
        assert combined == TokenUsage(input_tokens=11, output_tokens=2, cache_read_tokens=5)

    def test_empty_totals_zero(self):
        assert TokenUsage().total == 0


class TestClaudeUsage:
    def test_reads_the_anthropic_wire_shape(self):
        usage = claude_adapter.parse_usage(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 20,
                },
            }
        )
        assert usage == TokenUsage(
            input_tokens=100, output_tokens=50, cache_read_tokens=900, cache_creation_tokens=20
        )

    def test_cache_tokens_stay_separate_from_input(self):
        # They are priced differently; folding them together would misreport exactly the
        # thing this measures.
        usage = claude_adapter.parse_usage(
            {"usage": {"input_tokens": 10, "cache_read_input_tokens": 1000}}
        )
        assert usage.input_tokens == 10
        assert usage.cache_read_tokens == 1000

    def test_no_usage_field_is_none_not_zero(self):
        assert claude_adapter.parse_usage({"type": "result", "total_cost_usd": 1.0}) is None

    def test_a_non_dict_usage_field_is_none(self):
        assert claude_adapter.parse_usage({"usage": "nonsense"}) is None

    def test_an_empty_usage_object_is_none(self):
        assert claude_adapter.parse_usage({"usage": {}}) is None

    def test_partial_usage_keeps_what_it_found(self):
        assert claude_adapter.parse_usage({"usage": {"output_tokens": 7}}) == TokenUsage(
            output_tokens=7
        )

    def test_a_boolean_is_not_a_token_count(self):
        # bool is a subclass of int, so an unguarded read turns JSON `true` into 1 token.
        assert claude_adapter.parse_usage({"usage": {"input_tokens": True}}) is None

    def test_a_genuine_zero_is_reported_as_zero(self):
        assert claude_adapter.parse_usage({"usage": {"input_tokens": 0}}) == TokenUsage(
            input_tokens=0
        )


class TestGrokUsage:
    def test_reads_the_same_wire_shape_as_claude(self):
        # Grok emits the Anthropic Messages API format -- see its module docstring.
        usage = grok_adapter.parse_usage(
            {"usage": {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7}}
        )
        assert usage == TokenUsage(input_tokens=5, output_tokens=6, cache_read_tokens=7)

    def test_no_usage_field_is_none(self):
        assert grok_adapter.parse_usage({"type": "result"}) is None


class TestCodexUsage:
    def test_reads_usage_off_the_turn_completed_event(self):
        stream = _stream(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
            {"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 12}},
        )
        assert codex_adapter.find_usage(stream) == TokenUsage(input_tokens=30, output_tokens=12)

    def test_accepts_the_nested_turn_envelope(self):
        # The exact nesting is documented rather than verified, so both are accepted.
        stream = _stream({"type": "turn.completed", "turn": {"usage": {"input_tokens": 8}}})
        assert codex_adapter.find_usage(stream) == TokenUsage(input_tokens=8)

    def test_reads_cached_input_tokens_as_a_cache_read(self):
        stream = _stream({"type": "turn.completed", "usage": {"cached_input_tokens": 500}})
        assert codex_adapter.find_usage(stream) == TokenUsage(cache_read_tokens=500)

    def test_the_live_shape_is_read_field_for_field(self):
        # Captured verbatim from a real `codex exec --json` run.
        stream = _stream(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 13781,
                    "cached_input_tokens": 11008,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 0,
                },
            }
        )
        assert codex_adapter.find_usage(stream) == TokenUsage(
            input_tokens=2773, output_tokens=5, cache_read_tokens=11008
        )

    def test_the_cached_portion_is_subtracted_from_the_input_total(self):
        # Codex renames the Responses API's usage field for field and keeps its semantics:
        # `input_tokens` is the total *including* the cached part. Anthropic's means the
        # fresh remainder. Storing Codex's number under Anthropic's meaning would report
        # 24,789 input tokens for the turn above instead of 13,781, and halve its cache rate.
        stream = _stream(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 600,
                    "cache_write_input_tokens": 300,
                },
            }
        )
        assert codex_adapter.find_usage(stream) == TokenUsage(
            input_tokens=100, cache_read_tokens=600, cache_creation_tokens=300
        )

    def test_cache_writes_are_no_longer_dropped(self):
        # `cache_write_input_tokens` was absent from the alias table until a live capture
        # showed it, so every Codex cycle reported zero cache writes.
        stream = _stream({"type": "turn.completed", "usage": {"cache_write_input_tokens": 4096}})
        assert codex_adapter.find_usage(stream).cache_creation_tokens == 4096

    def test_a_total_that_would_go_negative_is_clamped(self):
        # Defensive: never report a negative token count if the CLI's semantics ever change.
        stream = _stream(
            {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 90}}
        )
        assert codex_adapter.find_usage(stream).input_tokens == 0

    def test_the_last_turn_wins(self):
        stream = _stream(
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
            {"type": "turn.completed", "usage": {"input_tokens": 2}},
        )
        assert codex_adapter.find_usage(stream) == TokenUsage(input_tokens=2)

    def test_a_stream_with_no_turn_completed_is_none(self):
        stream = _stream({"type": "item.completed", "item": {"type": "agent_message"}})
        assert codex_adapter.find_usage(stream) is None

    def test_reports_nothing_when_the_shape_is_unfamiliar(self):
        # The safety property: an unrecognised payload degrades to "no data", which renders
        # as `-`, rather than to a wrong number.
        stream = _stream({"type": "turn.completed", "usage": {"totally_unexpected": 42}})
        assert codex_adapter.find_usage(stream) is None

    def test_survives_non_json_and_malformed_lines(self):
        stream = (
            "warming up\n"
            "{not json at all\n"
            + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}})
            + "\n"
        )
        assert codex_adapter.find_usage(stream) == TokenUsage(input_tokens=3)


class TestCopilotUsage:
    def test_reads_usage_off_the_result_event(self):
        stream = _stream(
            {"type": "assistant.message", "data": {"content": "done"}},
            {"type": "result", "data": {"usage": {"inputTokens": 40, "outputTokens": 9}}},
        )
        assert copilot_adapter.find_usage(stream) == TokenUsage(input_tokens=40, output_tokens=9)

    def test_does_not_look_at_the_assistant_message_event(self):
        # parse_cli_output returns the last assistant.message, a *different* event -- reading
        # usage off that would always find nothing, which is why this is its own scan.
        stream = _stream({"type": "assistant.message", "data": {"content": "done"}})
        assert copilot_adapter.find_usage(stream) is None

    def test_accepts_snake_case_too(self):
        stream = _stream({"type": "result", "data": {"usage": {"input_tokens": 11}}})
        assert copilot_adapter.find_usage(stream) == TokenUsage(input_tokens=11)

    def test_accepts_usage_at_the_top_level(self):
        stream = _stream({"type": "result", "usage": {"inputTokens": 12}})
        assert copilot_adapter.find_usage(stream) == TokenUsage(input_tokens=12)

    def test_reports_nothing_when_the_shape_is_unfamiliar(self):
        stream = _stream({"type": "result", "data": {"usage": {"premiumRequests": 3}}})
        assert copilot_adapter.find_usage(stream) is None

    def test_survives_non_json_lines(self):
        stream = (
            "starting\n"
            + json.dumps({"type": "result", "data": {"usage": {"inputTokens": 4}}})
            + "\n"
        )
        assert copilot_adapter.find_usage(stream) == TokenUsage(input_tokens=4)
