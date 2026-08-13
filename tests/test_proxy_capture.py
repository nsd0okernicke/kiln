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
    extract_composition,
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


def _response_completed(**usage):
    """The Responses API's terminal event, as Codex receives it."""
    return {"type": "response.completed", "response": {"usage": usage}}


class TestRedactHeaders:
    def test_authorization_never_survives(self):
        assert redact_headers({"Authorization": "Bearer sk-secret"})["Authorization"] == REDACTED

    def test_codex_identity_headers_are_withheld(self):
        # Not bearer credentials, but a stable account identifier and an installation/session
        # id block, found in a live Codex capture. Nothing in Kiln reads them.
        headers = redact_headers({
            "chatgpt-account-id": "9016869b-67b8-45fb-8441-75d7da2f1b06",
            "x-codex-turn-metadata": '{"installation_id":"314c0fad"}',
        })
        assert set(headers.values()) == {REDACTED}

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


class TestExtractComposition:
    """
    The split that redirects the whole optimization argument.

    Measured on a real cycle: `tools` 28-34KB, `system` (the generated worker instructions)
    5-6KB, `messages` 30-92KB. The intuitive target -- slimming the worker instructions --
    is ~5% of a request; the conversation is 60-70%.
    """

    def test_measures_each_section(self):
        body = json.dumps({
            "model": "claude-sonnet-5",
            "tools": [{"name": "Read", "description": "x" * 100}],
            "system": "y" * 50,
            "messages": [{"role": "user", "content": "z" * 200}],
        })
        composition = extract_composition(body)
        assert set(composition) == {"tools", "system", "messages"}
        assert composition["messages"] > composition["system"]

    def test_absent_sections_are_omitted_not_zero(self):
        composition = extract_composition(json.dumps({"model": "m", "messages": []}))
        assert "messages" in composition
        assert "tools" not in composition

    def test_a_truncated_body_yields_nothing(self):
        # Capture truncates at the body limit, which leaves invalid JSON. Better no numbers
        # than numbers computed from a clipped request.
        assert extract_composition('{"tools": [{"name": "Read"') == {}

    def test_no_body_yields_nothing(self):
        assert extract_composition(None) == {}
        assert extract_composition("") == {}

    def test_a_non_object_body_yields_nothing(self):
        assert extract_composition("[1, 2, 3]") == {}

    def test_sections_never_sum_to_more_than_the_request(self):
        # The bug this pins: measuring with json.dumps defaults added `", "`/`": "` padding
        # the client never sent, so the three sections summed past 100% of the body they
        # were extracted from.
        body = json.dumps(
            {
                "model": "claude-sonnet-5",
                "tools": [{"name": "Read", "description": "x" * 100}],
                "system": "y" * 50,
                "messages": [{"role": "user", "content": "z" * 200}],
            },
            separators=(",", ":"),
        )
        assert sum(extract_composition(body).values()) <= len(body.encode("utf-8"))

    def test_non_ascii_is_measured_as_utf8_bytes(self):
        # Two bytes on the wire, not the six of an \\uXXXX escape.
        composition = extract_composition(json.dumps({"system": "é"}, ensure_ascii=False))
        assert composition["system"] == len('"é"'.encode())


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


class TestResponsesApiUsage:
    """
    Codex talks the OpenAI Responses API, whose usage object means something different from
    Anthropic's despite sharing two key names. Verified against a live Codex call.
    """

    def test_the_live_codex_shape_reproduces_the_cli_s_own_total(self):
        # Captured verbatim from a real `codex exec` through the proxy. Codex printed
        # "tokens used 10.895"; input + output must come to exactly that.
        usage = extract_usage(_sse(_response_completed(
            input_tokens=10_890,
            input_tokens_details={"cache_write_tokens": 0, "cached_tokens": 0},
            output_tokens=5,
            output_tokens_details={"reasoning_tokens": 0},
            total_tokens=10_895,
        )))
        assert usage.total == 10_895

    def test_cached_tokens_are_subtracted_from_the_input_total(self):
        # OpenAI's `input_tokens` INCLUDES the cached portion; Anthropic's excludes it.
        # Storing the raw number would double-count and halve the reported cache hit rate.
        body = _sse(_response_completed(
            input_tokens=10_000, output_tokens=50,
            input_tokens_details={"cached_tokens": 9_000},
        ))
        assert extract_usage(body) == TokenUsage(
            input_tokens=1_000, output_tokens=50, cache_read_tokens=9_000
        )

    def test_no_cache_detail_reads_the_input_total_unchanged(self):
        body = _sse(_response_completed(input_tokens=120, output_tokens=8))
        assert extract_usage(body) == TokenUsage(input_tokens=120, output_tokens=8)

    def test_cache_writes_are_read_from_the_same_details_object(self):
        # Found by a live Codex call, which sent
        # `{"cached_tokens": 0, "cache_write_tokens": 0}` when this reader assumed the API
        # had no cache-write concept at all.
        body = _sse(_response_completed(
            input_tokens=1_000, output_tokens=3,
            input_tokens_details={"cached_tokens": 600, "cache_write_tokens": 300},
        ))
        assert extract_usage(body) == TokenUsage(
            input_tokens=100, output_tokens=3,
            cache_read_tokens=600, cache_creation_tokens=300,
        )

    def test_cache_creation_stays_zero_when_not_reported(self):
        body = _sse(_response_completed(
            input_tokens=500, input_tokens_details={"cached_tokens": 400}
        ))
        assert extract_usage(body).cache_creation_tokens == 0

    def test_a_failed_turn_still_reports_what_it_burned(self):
        body = _sse({"type": "response.failed", "response": {"usage": {"input_tokens": 77}}})
        assert extract_usage(body) == TokenUsage(input_tokens=77)

    def test_a_non_streamed_responses_reply_is_read_too(self):
        body = json.dumps({
            "object": "response",
            "usage": {
                "input_tokens": 300, "output_tokens": 12,
                "input_tokens_details": {"cached_tokens": 250},
            },
        })
        assert extract_usage(body) == TokenUsage(
            input_tokens=50, output_tokens=12, cache_read_tokens=250
        )

    def test_anthropic_usage_is_still_read_the_anthropic_way(self):
        # The dispatch must not regress the format that was working first.
        body = json.dumps({"usage": {"input_tokens": 10, "cache_read_input_tokens": 90}})
        assert extract_usage(body) == TokenUsage(input_tokens=10, cache_read_tokens=90)


class TestResponsesApiComposition:
    """
    The same three buckets out of a request that shares no key names with Anthropic's:
    no top-level `tools`, no `system`, no `messages` -- one flat `input` array instead.
    """

    def _request(self):
        return json.dumps({
            "model": "gpt-5.6-sol",
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": ["x" * 500]},
                {"type": "message", "role": "developer", "content": "i" * 200},
                {"type": "message", "role": "user", "content": "u" * 50},
            ],
            "stream": True,
        })

    def test_each_bucket_is_measured(self):
        composition = extract_composition(self._request())
        assert set(composition) == {"tools", "system", "messages"}
        assert composition["tools"] > composition["system"] > composition["messages"]

    def test_additional_tools_counts_as_tools_not_instructions(self):
        # It carries `role: developer` as well as its type. Bucketing it by role would hide
        # the largest section of a Codex request inside the smallest.
        composition = extract_composition(self._request())
        assert composition["tools"] > 500

    def test_top_level_instructions_count_as_system(self):
        body = json.dumps({"instructions": "s" * 300, "input": []})
        assert extract_composition(body)["system"] > 300

    def test_an_anthropic_request_still_uses_the_anthropic_reader(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "input": "ignored"})
        assert set(extract_composition(body)) == {"messages"}

    def test_empty_buckets_are_omitted_not_zero(self):
        body = json.dumps({"input": [{"type": "message", "role": "user", "content": "hi"}]})
        assert set(extract_composition(body)) == {"messages"}


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

    def test_a_responses_stream_is_parsed_across_chunk_boundaries_too(self):
        body = _sse(_response_completed(
            input_tokens=8_000, output_tokens=40,
            input_tokens_details={"cached_tokens": 7_500},
        ))
        tracker = StreamingUsageTracker()
        for index in range(0, len(body), 7):
            tracker.feed(body[index:index + 7].encode("utf-8"))
        assert tracker.usage == TokenUsage(
            input_tokens=500, output_tokens=40, cache_read_tokens=7_500
        )

    def test_a_terminal_responses_event_wins_over_partial_anthropic_state(self):
        # Defensive: the two formats never mix on a real connection, but a complete report
        # is authoritative and must not be blended with a half-read one.
        tracker = StreamingUsageTracker()
        tracker.feed(_sse(_message_delta(999)).encode("utf-8"))
        tracker.feed(_sse(_response_completed(input_tokens=5, output_tokens=2)).encode("utf-8"))
        assert tracker.usage == TokenUsage(input_tokens=5, output_tokens=2)


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

    def test_an_older_store_gains_the_new_columns(self, tmp_path):
        """
        `CREATE TABLE IF NOT EXISTS` never alters an existing table.

        Without a migration, a store created before the composition columns existed keeps
        the old shape and every insert naming them fails -- the same trap
        `scheduler.db.ensure_schema` documents for the message queue.
        """
        import sqlite3
        path = tmp_path / "traffic.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                " role TEXT, method TEXT NOT NULL, path TEXT NOT NULL, model TEXT,"
                " status_code INTEGER, duration_ms INTEGER,"
                " request_bytes INTEGER NOT NULL DEFAULT 0,"
                " response_bytes INTEGER NOT NULL DEFAULT 0,"
                " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
                " cache_creation_tokens INTEGER, request_headers TEXT, request_body TEXT,"
                " response_body TEXT)"
            )
            conn.execute(
                "INSERT INTO traffic (ts, role, method, path, request_bytes) "
                "VALUES ('2026-08-13T07:00:00Z', 'coder', 'POST', '/v1/messages', 500)"
            )

        store = TrafficStore(path)
        store.ensure_schema()
        store.record(TrafficRecord(
            role="coder", method="POST", path="/v1/messages",
            composition={"tools": 10, "system": 20, "messages": 30},
        ))

        stats = store.request_stats_by_role()["coder"]
        assert stats["requests"] == 2, "the pre-existing row must survive the migration"
        assert stats["avg_tools"] == 10  # averaged over the row that has data

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
        assert stats["requests"] == 2
        assert stats["avg_bytes"] == 200
        assert stats["max_bytes"] == 300
        assert stats["total_bytes"] == 400

    def test_request_stats_are_empty_before_any_traffic(self, tmp_path):
        assert self._store(tmp_path).request_stats_by_role() == {}

    def test_request_stats_on_a_missing_store_are_empty(self, tmp_path):
        assert TrafficStore(tmp_path / "absent.db").request_stats_by_role() == {}

    def test_a_store_without_the_composition_columns_still_reports_sizes(self, tmp_path):
        """
        Degrade per column, not per panel.

        Observed live: naming `tools_bytes` against a store written by a proxy that
        predated it raised, the blanket except swallowed it, and the entire prompt-weight
        panel vanished from the dashboard -- taking the request-size figures with it, which
        need nothing new.
        """
        import sqlite3
        path = tmp_path / "traffic.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,"
                " role TEXT, method TEXT, path TEXT, request_bytes INTEGER)"
            )
            conn.execute(
                "INSERT INTO traffic (ts, role, method, path, request_bytes) "
                "VALUES ('2026-08-13T07:00:00Z', 'coder', 'POST', '/v1/messages', 1500)"
            )

        stats = TrafficStore(path).request_stats_by_role()["coder"]
        assert stats["requests"] == 1
        assert stats["avg_bytes"] == 1500      # still available
        assert stats["avg_tools"] is None      # honestly absent

    def test_request_stats_on_an_unreadable_store_are_empty(self, tmp_path):
        # An optional panel must not be able to take the dashboard down.
        junk = tmp_path / "traffic.db"
        junk.write_text("not a database", encoding="utf-8")
        assert TrafficStore(junk).request_stats_by_role() == {}

    def test_composition_is_recorded_and_averaged(self, tmp_path):
        store = self._store(tmp_path)
        for tools in (1000, 3000):
            store.record(TrafficRecord(
                role="coder", method="POST", path="/v1/messages", request_bytes=10_000,
                composition={"tools": tools, "system": 500, "messages": 4000},
            ))
        stats = store.request_stats_by_role()["coder"]
        assert stats["avg_tools"] == 2000
        assert stats["avg_system"] == 500
        assert stats["avg_messages"] == 4000

    def test_composition_survives_metadata_mode(self, tmp_path):
        # The point of computing it at capture time: the most useful analysis costs nothing
        # in stored prompt text.
        store = self._store(tmp_path)
        secret = json.dumps({"tools": [{"name": "Read"}], "system": "PROPRIETARY",
                             "messages": [{"content": "SECRET SOURCE"}]})
        store.record(TrafficRecord(
            role="coder", method="POST", path="/v1/messages",
            composition=extract_composition(secret),
            request_body=capture_body(secret, CaptureMode.METADATA),
        ))
        assert store.request_stats_by_role()["coder"]["avg_system"] > 0
        assert "SECRET SOURCE" not in (tmp_path / "traffic.db").read_bytes().decode("latin-1")

    def test_rows_without_composition_report_none_not_zero(self, tmp_path):
        store = self._store(tmp_path)
        store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages"))
        assert store.request_stats_by_role()["coder"]["avg_tools"] is None

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
