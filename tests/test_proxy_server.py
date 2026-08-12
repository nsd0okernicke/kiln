"""
The proxy relaying real HTTP — issue #6 Phase B.

These run a fake upstream and a real proxy over real sockets, so the relay is exercised
end to end **without any vendor quota**: no API key, no account, no spend. What they cannot
prove is the one thing that needs a live call — that Claude Code's own OAuth/subscription
auth survives a base-URL override. That check is noted in the issue and is the reason the
proxy is not yet wired into the launcher.

The streaming test is the one that matters most. A proxy that buffered a response to inspect
it would turn every worker pane silent for minutes, destroying the live output that
`--output-format stream-json` was chosen to provide.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from proxy.capture import CaptureMode, TrafficStore
from proxy.server import serve, split_role
from scheduler.adapters import TokenUsage

pytestmark = pytest.mark.integration

#: Long enough that a buffering proxy is unmistakable, short enough not to slow the suite.
STREAM_GAP_SEC = 0.4


def _sse(event):
    return f"data: {json.dumps(event)}\n\n".encode()


class _FakeUpstream(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com: streams two SSE events with a gap between them."""

    #: What the last request carried, so tests can assert on what actually reached upstream.
    #: A ClassVar because the handler is instantiated per request by the stdlib server —
    #: there is no instance for a test to hold on to.
    received: ClassVar[dict] = {}

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received = {
            "path": self.path,
            "body": body.decode("utf-8"),
            "headers": dict(self.headers.items()),
        }

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            _sse({"type": "message_start",
                  "message": {"usage": {"input_tokens": 120, "cache_read_input_tokens": 900}}})
        )
        self.wfile.flush()
        time.sleep(STREAM_GAP_SEC)
        self.wfile.write(_sse({"type": "message_delta", "usage": {"output_tokens": 42}}))
        self.wfile.flush()


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def proxy(tmp_path, upstream):
    store = TrafficStore(tmp_path / "traffic.db")
    host, port = upstream.server_address[0], upstream.server_address[1]
    server = serve(
        store=store, port=0, upstream=f"{host}:{port}",
        mode=CaptureMode.METADATA, use_tls=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store
    server.shutdown()


def _post(server, path, payload=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    connection.request(
        "POST", path,
        body=json.dumps(payload or {"model": "claude-sonnet-5", "messages": []}),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return connection


class TestSplitRole:
    def test_extracts_the_role_and_strips_the_prefix(self):
        assert split_role("/kiln/coder/v1/messages") == ("coder", "/v1/messages")

    def test_a_path_without_the_prefix_is_unattributed_and_unchanged(self):
        # An unattributed capture still beats a failed request.
        assert split_role("/v1/messages") == (None, "/v1/messages")

    def test_a_role_with_no_trailing_path_becomes_root(self):
        assert split_role("/kiln/coder") == ("coder", "/")

    def test_an_empty_role_is_not_attributed(self):
        assert split_role("/kiln//v1/messages") == (None, "/kiln//v1/messages")

    def test_hyphenated_roles_survive(self):
        # `human-in-the-loop` has broken naive role parsing before (see routing.py).
        assert split_role("/kiln/human-in-the-loop/v1/messages") == (
            "human-in-the-loop", "/v1/messages"
        )


class TestRelay:
    def test_the_response_reaches_the_client_intact(self, proxy):
        server, _ = proxy
        response = _post(server, "/kiln/coder/v1/messages").getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "message_start" in body
        assert "message_delta" in body

    def test_the_role_prefix_is_stripped_before_forwarding(self, proxy):
        server, _ = proxy
        _post(server, "/kiln/coder/v1/messages").getresponse().read()
        # Upstream must see the real API path, not Kiln's routing prefix.
        assert _FakeUpstream.received["path"] == "/v1/messages"

    def test_the_request_body_is_forwarded_unchanged(self, proxy):
        server, _ = proxy
        payload = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        _post(server, "/kiln/coder/v1/messages", payload).getresponse().read()
        assert json.loads(_FakeUpstream.received["body"]) == payload

    def test_credentials_are_forwarded_untouched(self, proxy):
        # The proxy must be invisible to auth -- it only refuses to *record* the credential.
        server, _ = proxy
        _post(
            server, "/kiln/coder/v1/messages", headers={"x-api-key": "sk-ant-secret"}
        ).getresponse().read()
        assert _FakeUpstream.received["headers"]["x-api-key"] == "sk-ant-secret"


class TestStreaming:
    def test_the_first_event_arrives_before_the_last_is_sent(self, proxy):
        """
        The load-bearing property: no buffering between model and pane.

        The fake upstream sleeps `STREAM_GAP_SEC` between its two events. If the proxy
        buffered, the first byte would only appear after that gap.
        """
        server, _ = proxy
        started = time.monotonic()
        response = _post(server, "/kiln/coder/v1/messages").getresponse()

        first_chunk = response.read(1)
        first_byte_at = time.monotonic() - started
        response.read()

        assert first_chunk, "no data reached the client at all"
        assert first_byte_at < STREAM_GAP_SEC, (
            f"first byte took {first_byte_at:.2f}s with a {STREAM_GAP_SEC}s upstream gap "
            "-- the proxy is buffering the response"
        )


def _rows(store):
    import sqlite3
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM traffic")]


def _wait_for_rows(store, count=1, timeout=5.0):
    """
    Poll until `count` rows land, or fail loudly.

    Recording happens *after* the last byte reaches the client -- deliberately, since the
    response size is not known until then -- so the client returning is not a guarantee the
    row exists yet. Polling makes that ordering explicit instead of flaky.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(store)
        if len(rows) >= count:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"expected {count} traffic row(s), found {len(_rows(store))}")


class TestCapture:
    def _recorded(self, store):
        return _wait_for_rows(store)

    def test_the_exchange_is_attributed_to_its_role(self, proxy):
        server, store = proxy
        _post(server, "/kiln/refactorer/v1/messages").getresponse().read()
        assert self._recorded(store)[0]["role"] == "refactorer"

    def test_usage_is_parsed_out_of_the_streamed_response(self, proxy):
        server, store = proxy
        _post(server, "/kiln/coder/v1/messages").getresponse().read()
        row = self._recorded(store)[0]
        assert row["input_tokens"] == 120
        assert row["cache_read_tokens"] == 900
        assert row["output_tokens"] == 42

    def test_the_model_is_recorded_from_the_request(self, proxy):
        server, store = proxy
        _post(server, "/kiln/coder/v1/messages").getresponse().read()
        assert self._recorded(store)[0]["model"] == "claude-sonnet-5"

    def test_sizes_and_status_are_recorded(self, proxy):
        server, store = proxy
        _post(server, "/kiln/coder/v1/messages").getresponse().read()
        row = self._recorded(store)[0]
        assert row["status_code"] == 200
        assert row["request_bytes"] > 0
        assert row["response_bytes"] > 0

    def test_the_credential_is_never_written_to_the_store(self, proxy):
        server, store = proxy
        _post(
            server, "/kiln/coder/v1/messages", headers={"x-api-key": "sk-ant-supersecret"}
        ).getresponse().read()
        _wait_for_rows(store)
        on_disk = store.db_path.read_bytes().decode("latin-1")
        assert "sk-ant-supersecret" not in on_disk

    def test_metadata_mode_stores_no_bodies(self, proxy):
        server, store = proxy
        payload = {"model": "claude-sonnet-5", "messages": [{"content": "SENSITIVE SOURCE"}]}
        _post(server, "/kiln/coder/v1/messages", payload).getresponse().read()
        row = self._recorded(store)[0]
        assert row["request_body"] is None
        assert row["response_body"] is None
        assert "SENSITIVE SOURCE" not in store.db_path.read_bytes().decode("latin-1")

    def test_totals_by_role_aggregate_real_traffic(self, proxy):
        server, store = proxy
        for _ in range(2):
            _post(server, "/kiln/coder/v1/messages").getresponse().read()
        _wait_for_rows(store, count=2)
        assert store.totals_by_role()["coder"] == TokenUsage(
            input_tokens=240, output_tokens=84, cache_read_tokens=1800
        )


class TestStubMode:
    """
    Capture without contacting the vendor at all.

    This is what makes the "does the CLI send its credential here" question answerable for
    free — which matters when the thing being measured is spend.
    """

    @pytest.fixture
    def stub_proxy(self, tmp_path):
        store = TrafficStore(tmp_path / "traffic.db")
        # Upstream deliberately points at a port with nothing on it: if stub mode ever
        # forwarded, the test would fail rather than silently pass.
        server = serve(
            store=store, port=0, upstream="127.0.0.1:1", mode=CaptureMode.METADATA,
            use_tls=False, stub=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server, store
        server.shutdown()

    def test_answers_without_forwarding(self, stub_proxy):
        server, _ = stub_proxy
        response = _post(server, "/kiln/spike/v1/messages").getresponse()
        assert response.status == 200
        assert "kiln proxy stub" in response.read().decode("utf-8")

    def test_the_reply_is_a_parseable_anthropic_stream(self, stub_proxy):
        # Structurally valid, so a real client parses it and the question under test stays
        # "what did the client send", not "did our stub confuse it".
        server, _ = stub_proxy
        body = _post(server, "/kiln/spike/v1/messages").getresponse().read().decode("utf-8")
        kinds = [
            json.loads(line[len("data:"):])["type"]
            for line in body.splitlines()
            if line.startswith("data:")
        ]
        assert kinds[0] == "message_start"
        assert kinds[-1] == "message_stop"

    def test_every_data_line_is_preceded_by_a_named_event_line(self, stub_proxy):
        """
        The Anthropic stream is named-event SSE, not data-only.

        Verified the hard way: a data-only stub made Claude Code fail outright with "API
        returned an empty or malformed response (HTTP 200)". A stub no real client accepts
        can only answer half the question it exists for.
        """
        server, _ = stub_proxy
        body = _post(server, "/kiln/spike/v1/messages").getresponse().read().decode("utf-8")
        blocks = [block for block in body.split("\n\n") if block.strip()]
        assert blocks, "stub produced no SSE blocks"
        for block in blocks:
            lines = block.strip().splitlines()
            assert lines[0].startswith("event: "), f"missing event line in: {block!r}"
            assert lines[1].startswith("data: ")
            assert lines[0][len("event: "):] == json.loads(lines[1][len("data: "):])["type"]

    def test_the_request_is_still_captured(self, stub_proxy):
        server, store = stub_proxy
        _post(server, "/kiln/spike/v1/messages").getresponse().read()
        row = _wait_for_rows(store)[0]
        assert row["role"] == "spike"
        assert row["model"] == "claude-sonnet-5"

    def test_the_credential_header_is_recorded_by_name_only(self, stub_proxy):
        # The whole point of the spike: identify *which* auth scheme was attached without
        # ever writing the secret to disk.
        server, store = stub_proxy
        _post(
            server, "/kiln/spike/v1/messages", headers={"x-api-key": "sk-ant-supersecret"}
        ).getresponse().read()
        row = _wait_for_rows(store)[0]
        headers = json.loads(row["request_headers"])
        assert "x-api-key" in {name.lower() for name in headers}
        assert "sk-ant-supersecret" not in store.db_path.read_bytes().decode("latin-1")


class TestFullCaptureMode:
    def test_bodies_are_stored_when_explicitly_enabled(self, tmp_path, upstream):
        store = TrafficStore(tmp_path / "traffic.db")
        host, port = upstream.server_address
        server = serve(
            store=store, port=0, upstream=f"{host}:{port}",
            mode=CaptureMode.FULL, use_tls=False,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {"model": "claude-sonnet-5", "messages": [{"content": "the prompt"}]}
            _post(server, "/kiln/coder/v1/messages", payload).getresponse().read()
            row = _wait_for_rows(store)[0]
        finally:
            server.shutdown()

        assert "the prompt" in row["request_body"]
        assert "message_start" in row["response_body"]
