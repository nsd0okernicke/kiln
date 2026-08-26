"""
The real network fetch, against a real loopback server.

The rest of the url path is tested through a fake fetcher, which is what keeps it fast and
offline -- but that leaves the one module that actually opens a socket unexercised, and an
untested fetcher is exactly where a timeout, a size cap or a scheme check quietly stops working.
A local `http.server` on an ephemeral port is deterministic local infrastructure, the same
category as the SQLite tests.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kiln.knowledge.domain.models import KnowledgeError
from kiln.knowledge.infrastructure.http_fetcher import OfflineFetcher, UrllibFetcher

pytestmark = pytest.mark.integration

BODY = b"<html><body><h1>Limits</h1><p>100 per minute.</p></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        if self.path == "/redirect-to-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        body = BODY * (200_000 if self.path == "/huge" else 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        return


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_fetches_a_page_and_reports_its_content_type(server):
    resource = UrllibFetcher().fetch(f"{server}/limits.html")

    assert resource.content == BODY
    assert resource.content_type.startswith("text/html")
    assert resource.url == f"{server}/limits.html"


def test_an_http_error_becomes_a_knowledge_error_rather_than_an_exception_from_urllib(server):
    """Every failure has to arrive as the one exception `sync` knows how to record."""
    with pytest.raises(KnowledgeError, match="could not fetch"):
        UrllibFetcher().fetch(f"{server}/missing")


def test_an_unreachable_host_is_reported_not_raised_raw():
    with pytest.raises(KnowledgeError, match="could not fetch"):
        UrllibFetcher(timeout=2).fetch("http://127.0.0.1:9/unused")


def test_an_oversized_response_is_refused_by_size_not_read_whole(server):
    """A wrong url pointing at something enormous must not be pulled into memory."""
    with pytest.raises(KnowledgeError, match="larger than"):
        UrllibFetcher(max_bytes=1024).fetch(f"{server}/huge")


def test_a_redirect_to_another_scheme_is_refused(server):
    """
    The rule that makes url sources safe: without it an http source could be bounced to
    `file://` and become an arbitrary local read that skips project containment entirely.
    """
    with pytest.raises(KnowledgeError, match=r"could not fetch|must be http"):
        UrllibFetcher().fetch(f"{server}/redirect-to-file")


def test_a_non_http_scheme_never_reaches_the_network():
    with pytest.raises(KnowledgeError, match="must be http"):
        UrllibFetcher().fetch("file:///etc/passwd")


def test_the_offline_fetcher_advertises_itself_as_unavailable():
    """`supports()` reads this; a fetcher that lied here would defer nothing."""
    assert OfflineFetcher().available is False
    assert UrllibFetcher().available is True
