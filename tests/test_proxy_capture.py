"""
What the proxy keeps, and what it refuses to keep — issue #6 Phase B.

The refusals carry most of the weight here. A capture store holds the entire source an
agent read, in plaintext, in a directory symlinked into every worktree, so "the credential
never reaches the store" and "bodies are opt-in and bounded" are the properties worth
pinning hardest.
"""

from __future__ import annotations

import json

import pytest
from proxy.capture import (
    DEFAULT_BODY_LIMIT_BYTES,
    REDACTED,
    TRUNCATION_MARKER,
    CaptureMode,
    StreamingUsageTracker,
    TrafficRecord,
    TrafficStore,
    capture_body,
    extract_model,
    extract_usage,
    redact_headers,
)
from scheduler.adapters import TokenUsage


def _sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def _message_start(**usage):
    return {"type": "message_start", "message": {"usage": usage}}


def _message_delta(output_tokens):
    return {"type": "message_delta", "usage": {"output_tokens": output_tokens}}


class TestRedactHeaders:
    def test_authorization_never_survives(self):
        assert redact_headers({"Authorization": "Bearer sk-secret"})["Authorization"] == REDACTED

    def test_api_key_never_survives(self):
        assert redact_headers({"x-api-key": "sk-ant-secret"})["x-api-key"] == REDACTED

    def test_matching_is_case_insensitive(self):
        # HTTP header names are case-insensitive; a proxy that only caught the lower-case
        # spelling would pass `Authorization` straight into the store.
        for spelling in ("authorization", "Authorization", "AUTHORIZATION"):
            assert redact_headers({spelling: "secret"})[spelling] == REDACTED

    def test_cookies_count_as_credentials(self):
        # A session cookie is a bearer credential in every way that matters.
        assert redact_headers({"Cookie": "session=abc"})["Cookie"] == REDACTED

    def test_proxy_authorization_is_covered(self):
        headers = redact_headers({"Proxy-Authorization": "Basic xyz"})
        assert headers["Proxy-Authorization"] == REDACTED

    def test_ordinary_headers_pass_through(self):
        headers = redact_headers({"content-type": "application/json", "anthropic-version": "1"})
        assert headers == {"content-type": "application/json", "anthropic-version": "1"}

    def test_presence_is_preserved(self):
        # `<redacted>` rather than dropping the key: "present and withheld" and "absent"
        # mean different things when debugging an auth failure through the proxy.
        assert "Authorization" in redact_headers({"Authorization": "Bearer x"})


class TestCaptureBody:
    def test_metadata_mode_keeps_nothing(self):
        assert capture_body("secret prompt", CaptureMode.METADATA) is None

    def test_full_mode_keeps_the_body(self):
        assert capture_body("the prompt", CaptureMode.FULL) == "the prompt"

    def test_full_mode_truncates_at_the_limit(self):
        body = "x" * (DEFAULT_BODY_LIMIT_BYTES + 500)
        captured = capture_body(body, CaptureMode.FULL)
        assert captured.endswith(TRUNCATION_MARKER)
        assert len(captured) == DEFAULT_BODY_LIMIT_BYTES + len(TRUNCATION_MARKER)

    def test_truncation_is_marked_so_nobody_measures_a_clipped_prompt(self):
        captured = capture_body("y" * 100, CaptureMode.FULL, limit=10)
        assert TRUNCATION_MARKER in captured

    def test_a_body_at_exactly_the_limit_is_not_truncated(self):
        body = "z" * 10
        assert capture_body(body, CaptureMode.FULL, limit=10) == body

    def test_absent_body_stays_none(self):
        assert capture_body(None, CaptureMode.FULL) is None


class TestExtractModel:
    def test_reads_the_model_field(self):
        assert extract_model(json.dumps({"model": "claude-sonnet-5"})) == "claude-sonnet-5"

    def test_malformed_json_is_none(self):
        assert extract_model("{not json") is None

    def test_missing_model_is_none(self):
        assert extract_model(json.dumps({"messages": []})) is None

    def test_empty_body_is_none(self):
        assert extract_model(None) is None
        assert extract_model("") is None


class TestExtractUsage:
    def test_reads_a_plain_json_response(self):
        body = json.dumps({"usage": {"input_tokens": 10, "output_tokens": 4}})
        assert extract_usage(body) == TokenUsage(input_tokens=10, output_tokens=4)

    def test_reads_cache_fields(self):
        body = json.dumps(
            {"usage": {"cache_read_input_tokens": 900, "cache_creation_input_tokens": 20}}
        )
        assert extract_usage(body) == TokenUsage(cache_read_tokens=900, cache_creation_tokens=20)

    def test_merges_an_sse_stream(self):
        body = _sse(
            _message_start(input_tokens=100, cache_read_input_tokens=900),
            _message_delta(5),
            _message_delta(42),
        )
        # The last message_delta wins -- it is cumulative, not incremental.
        assert extract_usage(body) == TokenUsage(
            input_tokens=100, output_tokens=42, cache_read_tokens=900
        )

    def test_a_stream_with_no_usage_is_none(self):
        assert extract_usage(_sse({"type": "ping"})) is None

    def test_the_done_sentinel_is_ignored(self):
        assert extract_usage("data: [DONE]\n\n") is None

    def test_empty_body_is_none(self):
        assert extract_usage("") is None


class TestStreamingUsageTracker:
    def test_parses_across_chunk_boundaries(self):
        # The whole point: a chunk can split a line anywhere, and the tracker must not
        # need the full body in memory to survive it.
        body = _sse(_message_start(input_tokens=7), _message_delta(3))
        tracker = StreamingUsageTracker()
        for index in range(0, len(body), 5):
            tracker.feed(body[index:index + 5].encode("utf-8"))
        assert tracker.usage == TokenUsage(input_tokens=7, output_tokens=3)

    def test_matches_the_whole_body_parser(self):
        body = _sse(
            _message_start(input_tokens=50, cache_read_input_tokens=1000), _message_delta(9)
        )
        tracker = StreamingUsageTracker()
        tracker.feed(body.encode("utf-8"))
        assert tracker.usage == extract_usage(body)

    def test_reports_none_before_anything_arrives(self):
        assert StreamingUsageTracker().usage is None

    def test_a_stream_with_no_usage_events_is_none(self):
        tracker = StreamingUsageTracker()
        tracker.feed(_sse({"type": "content_block_delta"}).encode("utf-8"))
        assert tracker.usage is None

    def test_malformed_json_is_skipped_not_fatal(self):
        tracker = StreamingUsageTracker()
        tracker.feed(b"data: {not json\n\n")
        tracker.feed(_sse(_message_delta(4)).encode("utf-8"))
        assert tracker.usage == TokenUsage(output_tokens=4)

    def test_invalid_utf8_does_not_raise(self):
        tracker = StreamingUsageTracker()
        tracker.feed(b"data: \xff\xfe garbage\n\n")
        assert tracker.usage is None


class TestTrafficRecord:
    def test_headers_are_redacted_even_when_set_directly(self):
        # Belt and braces: the server redacts at the boundary, but a record built anywhere
        # else must not be able to smuggle a credential into the store.
        entry = TrafficRecord(
            role="coder", method="POST", path="/v1/messages",
            request_headers={"Authorization": "Bearer leak"},
        )
        assert entry.request_headers["Authorization"] == REDACTED

    def test_a_timestamp_is_stamped_automatically(self):
        assert TrafficRecord(role=None, method="GET", path="/").ts.endswith("Z")


@pytest.mark.integration
class TestTrafficStore:
    def _store(self, tmp_path):
        store = TrafficStore(tmp_path / "traffic.db")
        store.ensure_schema()
        return store

    def test_creates_its_own_file_not_messages_db(self, tmp_path):
        # messages.db is live swarm state read by the dashboard, the inbox and the kiln-db
        # MCP server; request bodies have no business in it.
        self._store(tmp_path)
        assert (tmp_path / "traffic.db").is_file()
        assert not (tmp_path / "messages.db").exists()

    def test_ensure_schema_is_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        store.ensure_schema()  # must not raise

    def test_records_an_exchange(self, tmp_path):
        store = self._store(tmp_path)
        row_id = store.record(
            TrafficRecord(
                role="coder", method="POST", path="/v1/messages",
                status_code=200, request_bytes=120, response_bytes=4096,
                model="claude-sonnet-5",
                tokens=TokenUsage(input_tokens=100, cache_read_tokens=900),
            )
        )
        assert row_id > 0

    def test_totals_are_summed_per_role(self, tmp_path):
        store = self._store(tmp_path)
        for role, tokens in (
            ("coder", TokenUsage(input_tokens=10, cache_read_tokens=100)),
            ("coder", TokenUsage(input_tokens=5)),
            ("refactorer", TokenUsage(output_tokens=7)),
        ):
            store.record(TrafficRecord(role=role, method="POST", path="/v1/messages",
                                       tokens=tokens))
        totals = store.totals_by_role()
        assert totals["coder"] == TokenUsage(input_tokens=15, cache_read_tokens=100)
        assert totals["refactorer"] == TokenUsage(output_tokens=7)

    def test_unattributed_traffic_is_excluded_from_totals(self, tmp_path):
        store = self._store(tmp_path)
        store.record(TrafficRecord(role=None, method="POST", path="/v1/messages",
                                   tokens=TokenUsage(input_tokens=99)))
        assert store.totals_by_role() == {}

    def test_request_stats_summarise_prompt_weight_per_role(self, tmp_path):
        store = self._store(tmp_path)
        for size in (100, 300):
            store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages",
                                       request_bytes=size))
        stats = store.request_stats_by_role()["coder"]
        assert stats == {
            "requests": 2, "avg_bytes": 200, "max_bytes": 300, "total_bytes": 400
        }

    def test_request_stats_are_empty_before_any_traffic(self, tmp_path):
        assert self._store(tmp_path).request_stats_by_role() == {}

    def test_request_stats_on_a_missing_store_are_empty(self, tmp_path):
        assert TrafficStore(tmp_path / "absent.db").request_stats_by_role() == {}

    def test_request_stats_on_an_unreadable_store_are_empty(self, tmp_path):
        # An optional panel must not be able to take the dashboard down.
        junk = tmp_path / "traffic.db"
        junk.write_text("not a database", encoding="utf-8")
        assert TrafficStore(junk).request_stats_by_role() == {}

    def test_metadata_mode_leaves_no_prompt_text_on_disk(self, tmp_path):
        store = self._store(tmp_path)
        secret = "PROPRIETARY SOURCE CODE"
        store.record(
            TrafficRecord(
                role="coder", method="POST", path="/v1/messages",
                request_body=capture_body(secret, CaptureMode.METADATA),
                response_body=capture_body(secret, CaptureMode.METADATA),
            )
        )
        assert secret not in (tmp_path / "traffic.db").read_bytes().decode("latin-1")
