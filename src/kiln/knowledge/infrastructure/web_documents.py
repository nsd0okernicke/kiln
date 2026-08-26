"""
Remote knowledge sources: one catalogued URL, one indexed document.

Fetching goes through the `WebFetcher` port rather than `urllib` directly, so this adapter --
and everything downstream of it -- is testable without a socket. `http_fetcher` supplies the
real one.

Responses are memoised for the life of the adapter because a sync asks for a document's
fingerprint and then, if it changed, its content. Without the cache that is two requests per
URL per sync, which is both slow and rude to whoever is serving the page.
"""

from __future__ import annotations

from ..application.ports import FetchedResource, WebFetcher
from ..domain import documents, web
from ..domain.models import ExtractedDocument, KnowledgeError, Section, Source


class WebDocumentSource:
    """`DocumentSource` over catalogued URLs. Its key is the URL itself."""

    def __init__(self, fetcher: WebFetcher):
        self.fetcher = fetcher
        self._cache: dict[str, FetchedResource] = {}

    def supports(self, source: Source) -> bool:
        return self.fetcher.available

    def discover(self, source: Source) -> list[str]:
        """One URL is one document; there is no recursion and no link following."""
        return [web.validate_url(source.url)]

    def fingerprint(self, source: Source, key: str) -> str:
        return documents.fingerprint(self._fetch(key).content)

    def extract(self, source: Source, key: str) -> ExtractedDocument:
        resource = self._fetch(key)
        kind = web.media_type_for(resource.content_type)
        return documents.build_document(
            # The URL stands where a relative path stands for a local file: it is what a
            # citation has to name for anyone to check the claim.
            relative_path=key,
            title=source.title or web.default_title(key),
            source=source,
            kind=kind,
            sections=self._sections(resource, kind),
            raw=resource.content,
        )

    def candidates(self, configured: set[str]) -> list[dict]:
        """Nothing to discover: a URL has to be named by a human, never guessed."""
        return []

    def _fetch(self, url: str) -> FetchedResource:
        if url not in self._cache:
            self._cache[url] = self.fetcher.fetch(url)
        return self._cache[url]

    def _sections(self, resource: FetchedResource, kind: str) -> tuple[Section, ...]:
        if kind == "pdf":
            raise KnowledgeError(
                f"remote PDFs are not indexed yet; download {resource.url} and catalog the file"
            )
        text = documents.decode(resource.content, resource.url)
        if kind == "html":
            return web.html_sections(text)
        return documents.text_sections(text, kind)
