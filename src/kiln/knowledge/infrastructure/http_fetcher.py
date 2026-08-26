"""
The real network fetch, over the standard library.

`urllib` rather than a dependency: one GET of a documentation page needs no session handling,
and Kiln is installed on machines whose dependency set should stay small.

Three limits, all of them because this runs unattended during `kiln` launch:

* a **timeout**, so an unreachable host delays a launch by seconds rather than hanging it;
* a **size cap**, read incrementally, so a wrong URL pointing at an ISO cannot exhaust memory;
* **no redirect to another scheme**, so an `http(s)` source cannot be bounced to `file://` and
  turned into a local-file read that skips the project-containment rule.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from ..application.ports import FetchedResource
from ..domain import web
from ..domain.models import KnowledgeError

TIMEOUT_SECONDS = 15

#: Documentation pages are text. Anything past this is the wrong URL, not a big document.
MAX_BYTES = 8 * 1024 * 1024

USER_AGENT = "kiln-knowledge/1"


class SchemeRestrictedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but only ones that stay on http/https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        web.validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibFetcher:
    """`WebFetcher` over `urllib`."""

    available = True

    def __init__(self, timeout: int = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = urllib.request.build_opener(SchemeRestrictedRedirects)

    def fetch(self, url: str) -> FetchedResource:
        # The scheme is validated here and again on every redirect hop.
        request = urllib.request.Request(web.validate_url(url), headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(self.max_bytes + 1)
                content_type = response.headers.get("Content-Type", "text/html")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise KnowledgeError(f"could not fetch {url}: {exc}") from exc
        if len(content) > self.max_bytes:
            raise KnowledgeError(f"knowledge source is larger than {self.max_bytes} bytes: {url}")
        return FetchedResource(url=url, content=content, content_type=content_type)


class OfflineFetcher:
    """
    What `--offline` installs: fetching is unavailable, so remote sources are deferred.

    Deferred, emphatically not failed. A failed source has its documents dropped, so treating
    "the network is off" as "this source is broken" would delete every indexed page the moment
    someone synced on a train -- and the next online sync would have to fetch them all again.
    """

    available = False

    def fetch(self, url: str) -> FetchedResource:  # pragma: no cover - never called
        raise KnowledgeError(f"offline: not fetching {url}")
