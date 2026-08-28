"""
The forwarding proxy itself — the only part of this package that touches a socket.

An agent CLI is pointed here through its own base-URL environment variable; this server
forwards each request upstream and streams the response straight back, recording what went
past. No TLS interception, no generated CA, nothing in a system trust store.

Two properties are load-bearing and everything else bends around them:

**Streaming must survive the hop.** Claude Code requests `stream: true` and the scheduler
renders each event as it arrives — that live pane output is the product's most distinctive
feature (see `claude_adapter.build_command`'s note on choosing `stream-json` over `json`).
A proxy that buffered a response to inspect it would turn every worker into a silent pane
for minutes at a time. So chunks are relayed the instant they arrive and usage is parsed
incrementally as they pass.

**Attribution comes from the path.** A proxy sees HTTP requests, not roles, so without
something tying a request to a role the capture is an undifferentiated blob that cannot
answer "what did the refactorer cost". Each role is pointed at `http://host:port/kiln/<role>`
and the prefix is stripped before forwarding. That rides `AgentCommand.env`, which already
builds per-pane environment.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import ssl
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ...domain.capture import (
    DEFAULT_BODY_LIMIT_BYTES,
    SENSITIVE_HEADERS,
    CaptureMode,
    StreamingUsageTracker,
    TrafficRecord,
    capture_body,
    extract_composition,
    extract_model,
    extract_usage,
    redact_headers,
)
from ..persistence import TrafficStore

log = logging.getLogger(__name__)

DEFAULT_UPSTREAM = "api.anthropic.com"
DEFAULT_PORT = 8787


@dataclass(frozen=True)
class Upstream:
    """
    Where one role's traffic goes, and what path prefix it needs on arrival.

    A single `--upstream` host was enough while only Claude was routed. It stops being
    enough the moment a second backend joins: Codex authenticates against
    `chatgpt.com/backend-api/codex` while Claude uses `api.anthropic.com`, and one proxy
    serves both because roles -- not backends -- are what the path prefix identifies.

    `base_path` exists because the client's own path is relative to its configured base URL.
    Codex sends `POST /responses`; upstream that has to become
    `/backend-api/codex/responses`. Anthropic needs no prefix and gets an empty one.
    """

    host: str
    base_path: str = ""

    def target(self, path: str) -> str:
        return f"{self.base_path}{path}"


def parse_upstream(value: str) -> Upstream:
    """`chatgpt.com/backend-api/codex` -> `Upstream("chatgpt.com", "/backend-api/codex")`."""
    host, separator, rest = value.strip().strip("/").partition("/")
    return Upstream(host=host, base_path=(separator + rest) if separator else "")


#: Prefix that carries the role name, e.g. `/kiln/coder/v1/messages`.
ROLE_PREFIX = "/kiln/"

#: Relayed in this size; small enough that a token appears in the pane promptly, large
#: enough not to syscall per byte.
CHUNK_BYTES = 8192

#: Connection-level headers that must not be forwarded — they describe *this* hop, not the
#: message, and passing them on corrupts the next one. Per RFC 9110 §7.6.1.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

#: Request headers dropped when forwarding, for reasons specific to *this* proxy rather
#: than to HTTP framing (those are HOP_BY_HOP_HEADERS).
#:
#: `accept-encoding`: the client asks for `gzip, deflate, br, zstd`, and upstream obliges.
#: The relay itself survives that -- `Content-Encoding` passes through and the client
#: decompresses correctly -- but everything this proxy exists to *read* does not: the SSE
#: usage tracker sees gzip bytes and finds no events, and a captured response body is stored
#: as mojibake. Observed live: 113 requests, every token column empty, bodies beginning
#: `1f 8b 08`.
#:
#: Stripping the header is the fix rather than decompressing, because the client offers
#: `br` and `zstd` too and neither is decodable with the standard library on this project's
#: target version. Asking upstream for identity encoding makes every path readable with no
#: guessing. The cost is more bytes on the upstream hop, paid only when the proxy is
#: switched on, which is the right trade for a tool whose entire job is measurement.
STRIPPED_REQUEST_HEADERS = frozenset({"accept-encoding"})

#: The canned reply stub mode returns: a minimal but structurally valid Anthropic SSE
#: exchange, so a client parses it rather than erroring on malformed output and the
#: question under test stays "what did the client send", not "did our stub confuse it".
STUB_EVENTS = (
    {
        "type": "message_start",
        "message": {
            "id": "msg_kiln_stub",
            "type": "message",
            "role": "assistant",
            "model": "kiln-proxy-stub",
            "content": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "text_delta",
            "text": "kiln proxy stub: request captured, nothing forwarded",
        },
    },
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}},
    {"type": "message_stop"},
)


def split_role(path: str) -> tuple[str | None, str]:
    """
    `/kiln/coder/v1/messages` -> `("coder", "/v1/messages")`.

    A path without the prefix yields `(None, path)` and is forwarded unchanged, so the proxy
    stays usable for a client that has not been told about roles — an unattributed capture
    is still better than a failed request.
    """
    if not path.startswith(ROLE_PREFIX):
        return None, path
    remainder = path[len(ROLE_PREFIX) :]
    role, separator, rest = remainder.partition("/")
    if not role:
        return None, path
    return role, (separator + rest) if separator else "/"


class ProxyConfig:
    """Everything the handler needs, kept off the handler class itself."""

    def __init__(
        self,
        store: TrafficStore,
        upstream: str = DEFAULT_UPSTREAM,
        mode: CaptureMode = CaptureMode.METADATA,
        body_limit: int = DEFAULT_BODY_LIMIT_BYTES,
        use_tls: bool = True,
        stub: bool = False,
        routes: dict[str, Upstream] | None = None,
    ) -> None:
        self.store = store
        self.upstream = parse_upstream(upstream) if isinstance(upstream, str) else upstream
        #: Per-role overrides. A role absent from here uses `upstream`, so adding a second
        #: backend never changes where an existing role's traffic goes.
        self.routes = routes or {}
        self.mode = mode
        self.body_limit = body_limit
        #: False reaches a plain-HTTP upstream — an internal gateway, or a fake one in
        #: tests. The vendor API is always HTTPS, so this defaults to on.
        self.use_tls = use_tls
        #: True answers locally and never contacts the vendor — see `_answer_with_stub`.
        self.stub = stub

    def upstream_for(self, role: str | None) -> Upstream:
        return self.routes.get(role or "", self.upstream)


class ProxyHandler(BaseHTTPRequestHandler):
    """One request/response exchange, relayed and recorded."""

    # Keep-alive matters: a worker makes many calls in a session and a new TCP+TLS
    # handshake per call would show up as latency in the pane.
    protocol_version = "HTTP/1.1"

    config: ProxyConfig  # injected by `serve`

    def log_message(self, format: str, *args) -> None:
        """Route the stdlib's own access log into logging instead of stderr."""
        log.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._relay("GET")

    def do_POST(self) -> None:
        self._relay("POST")

    def do_DELETE(self) -> None:
        self._relay("DELETE")

    def _relay(self, method: str) -> None:
        started = time.monotonic()
        role, upstream_path = split_role(self.path)

        length = int(self.headers.get("Content-Length") or 0)
        request_bytes = self.rfile.read(length) if length else b""

        if self.config.stub:
            self._answer_with_stub(role, method, upstream_path, request_bytes, started)
            return

        try:
            response = self._forward(method, upstream_path, request_bytes, role)
        except (OSError, http.client.HTTPException) as exc:
            # A proxy failure must read as a proxy failure, not as the model refusing.
            log.error("upstream request failed: %s", exc)
            self.send_error(502, "upstream request failed")
            self._record(
                role,
                method,
                upstream_path,
                request_bytes,
                b"",
                None,
                started,
                status=502,
                usage=None,
            )
            return

        self._relay_response(response, role, method, upstream_path, request_bytes, started)

    def _answer_with_stub(
        self, role: str | None, method: str, path: str, request_bytes: bytes, started: float
    ) -> None:
        """
        Record the request and reply locally, without contacting the vendor at all.

        This exists for one specific question: does a CLI pointed at this proxy actually
        send its request here, and what credential does it attach when the host is not the
        vendor's? Both are answerable from the request alone, so forwarding is pure cost --
        stub mode makes that check free, which matters when the thing you are trying to
        measure is spend.

        It doubles as a dry run: the whole launcher wiring can be exercised end to end
        without an account, a key, or a single token.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for event in STUB_EVENTS:
            # `event:` line as well as `data:` -- the Anthropic stream is named-event SSE,
            # and a real client rejects a data-only stream outright ("API returned an empty
            # or malformed response (HTTP 200)", observed live against Claude Code). A stub
            # nobody's client accepts would only be able to answer half the question.
            payload = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
            self.wfile.write(payload)
            self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

        log.info(
            "stub: %s %s from role=%s, auth headers seen: %s",
            method,
            path,
            role or "-",
            ", ".join(sorted(self._auth_header_names())) or "NONE",
        )
        self._record(
            role,
            method,
            path,
            request_bytes,
            b"",
            0,
            started,
            status=200,
            usage=None,
        )

    def _auth_header_names(self) -> set[str]:
        """Which credential headers the client attached — names only, never values."""
        return {name.lower() for name in self.headers if name.lower() in SENSITIVE_HEADERS}

    def _forward(
        self, method: str, path: str, body: bytes, role: str | None = None
    ) -> http.client.HTTPResponse:
        """Open the upstream connection and send the request through unchanged."""
        upstream = self.config.upstream_for(role)
        connection = self._upstream_connection(upstream)
        headers = self._forward_headers()
        connection.request(method, upstream.target(path), body=body or None, headers=headers)
        return connection.getresponse()

    def _upstream_connection(self, upstream: Upstream) -> http.client.HTTPConnection:
        if self.config.use_tls:
            return http.client.HTTPSConnection(
                upstream.host, context=ssl.create_default_context(), timeout=600
            )
        return http.client.HTTPConnection(upstream.host, timeout=600)

    def _forward_headers(self) -> dict[str, str]:
        return {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
            and name.lower() not in STRIPPED_REQUEST_HEADERS
            and name.lower() != "host"
        }

    def _relay_response(
        self,
        response: http.client.HTTPResponse,
        role: str | None,
        method: str,
        path: str,
        request_bytes: bytes,
        started: float,
    ) -> None:
        """Stream the response to the client, parsing usage as it goes."""
        passthrough = self._response_headers(response)
        self.send_response(response.status)
        for name, value in passthrough:
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        readable = self._response_is_readable(response)
        captured, total, usage = self._stream_body(response, readable)
        self._finish_chunked_response()
        self._record(
            role,
            method,
            path,
            request_bytes,
            captured,
            total,
            started,
            status=response.status,
            usage=usage,
        )

    def _response_headers(self, response: http.client.HTTPResponse) -> list[tuple[str, str]]:
        return [
            (name, value)
            for name, value in response.getheaders()
            if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "content-length"
        ]

    def _response_is_readable(self, response: http.client.HTTPResponse) -> bool:
        encoding = (response.getheader("Content-Encoding") or "identity").lower()
        readable = encoding in ("", "identity")
        if not readable:
            log.warning(
                "response is %s-encoded; relaying it but skipping usage and body capture",
                encoding,
            )
        return readable

    def _stream_body(self, response, readable: bool):
        tracker = StreamingUsageTracker()
        captured = bytearray()
        total = 0
        while True:
            # `read1`, never `read`: `HTTPResponse.read(n)` blocks until it has all n bytes
            # or the connection closes, which would hold every SSE event until the buffer
            # filled -- reintroducing exactly the buffering this relay exists to avoid.
            # `read1` returns as soon as any data is available.
            chunk = response.read1(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if readable:
                self._capture_chunk(tracker, captured, chunk)
            if not self._write_chunk(chunk):
                break
        return bytes(captured), total, tracker.usage

    def _capture_chunk(self, tracker, captured: bytearray, chunk: bytes) -> None:
        tracker.feed(chunk)
        if len(captured) < self.config.body_limit:
            captured.extend(chunk[: self.config.body_limit - len(captured)])

    def _finish_chunked_response(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_chunk(self, chunk: bytes) -> bool:
        try:
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            log.warning("client disconnected mid-response; abandoning relay")
            return False

    def _record(
        self,
        role: str | None,
        method: str,
        path: str,
        request_bytes: bytes,
        response_captured: bytes,
        response_total: int | None,
        started: float,
        status: int | None,
        usage,
    ) -> None:
        """Persist the exchange. Never raises — a capture failure must not fail the call."""
        try:
            request_text = request_bytes.decode("utf-8", errors="replace")
            response_text = response_captured.decode("utf-8", errors="replace")
            entry = TrafficRecord(
                role=role,
                method=method,
                path=path,
                status_code=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                request_bytes=len(request_bytes),
                response_bytes=response_total or len(response_captured),
                model=extract_model(request_text),
                # Computed here, not at render time: the dashboard polls every couple of
                # seconds and re-parsing hundreds of 100KB bodies per frame would be
                # unusable. It also means composition survives metadata mode, where no
                # body is stored at all.
                composition=extract_composition(request_text),
                # The streaming tracker is authoritative; the whole-body parse is the
                # fallback for a non-streamed JSON reply, which has no SSE events at all.
                tokens=usage or extract_usage(response_text),
                request_body=capture_body(request_text, self.config.mode, self.config.body_limit),
                response_body=capture_body(response_text, self.config.mode, self.config.body_limit),
                request_headers=redact_headers(dict(self.headers.items())),
            )
            self.config.store.record(entry)
        except Exception:  # pragma: no cover - capture must never break the proxy
            log.exception("could not record traffic; the request itself was unaffected")


def serve(
    store: TrafficStore,
    port: int = DEFAULT_PORT,
    upstream: str = DEFAULT_UPSTREAM,
    mode: CaptureMode = CaptureMode.METADATA,
    host: str = "127.0.0.1",
    use_tls: bool = True,
    stub: bool = False,
    routes: dict[str, Upstream] | None = None,
) -> ThreadingHTTPServer:
    """
    Build a configured server. The caller owns `serve_forever`/`shutdown`.

    Bound to loopback by default and deliberately: this relays credentialed traffic, and a
    capture store full of prompts has no business being reachable from the network.

    `port=0` binds an ephemeral port; read it back from `server.server_address[1]`.
    """
    store.ensure_schema()
    config = ProxyConfig(
        store=store, upstream=upstream, mode=mode, use_tls=use_tls, stub=stub, routes=routes
    )

    handler = type("ConfiguredProxyHandler", (ProxyHandler,), {"config": config})
    return ThreadingHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiln local traffic proxy (issue #6).")
    parser.add_argument("--db-path", required=True, help="capture store, e.g. .kiln/traffic.db")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="ROLE=HOST[/BASE-PATH]",
        help=(
            "send one role's traffic to a different upstream. Repeatable — roles not named "
            "here use --upstream. Examples:\n"
            "  --route architect=chatgpt.com/backend-api/codex\n"
            "  --route coder=api.x.ai\n"
            "  --route specifier=api.githubcopilot.com\n"
            "Backends: claude (default), codex (chatgpt.com/backend-api/codex), "
            "grok (api.x.ai), copilot (api.githubcopilot.com), pi (provider-specific)."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in CaptureMode],
        default=CaptureMode.METADATA.value,
        help="metadata (default) records sizes/model/usage only; full also stores bodies",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help=(
            "answer locally and never contact the upstream: records what a client sent "
            "and which credential it attached, at zero cost. Use to verify wiring."
        ),
    )
    return parser


def parse_routes(values: list[str]) -> dict[str, Upstream]:
    """
    `["coder=chatgpt.com/backend-api/codex"]` -> `{"coder": Upstream(...)}`.

    A malformed entry raises rather than being skipped: a silently ignored route sends a
    role's credentialed traffic to the wrong vendor, which fails as a 401 far from its cause.
    """
    routes: dict[str, Upstream] = {}
    for value in values:
        role, separator, target = value.partition("=")
        if not separator or not role.strip() or not target.strip():
            raise ValueError(f"--route needs ROLE=HOST[/BASE-PATH], got {value!r}")
        routes[role.strip()] = parse_upstream(target)
    return routes


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entry point
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [kiln-proxy/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        routes = parse_routes(args.route)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    server = serve(
        store=TrafficStore(Path(args.db_path)),
        port=args.port,
        upstream=args.upstream,
        mode=CaptureMode(args.mode),
        host=args.host,
        stub=args.stub,
        routes=routes,
    )
    for role, upstream in sorted(routes.items()):
        log.info("  route: %s -> %s%s", role, upstream.host, upstream.base_path)
    log.info(
        "proxy listening on http://%s:%s -> %s (capture: %s)",
        args.host,
        args.port,
        "STUB (nothing is forwarded)" if args.stub else args.upstream,
        args.mode,
    )
    log.info("point a role at http://%s:%s/kiln/<role>", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
