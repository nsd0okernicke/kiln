"""
Sends each catalog source to the adapter that knows how to read it.

A source's `type` decides where it goes: `url` to the web adapter, everything else to the file
adapter. Kept as its own object rather than an `if` inside the service, so adding a third kind
of origin later is a new adapter and a new row here, not an edit to a use case.
"""

from __future__ import annotations

from ..application.ports import DocumentSource
from ..domain.models import ExtractedDocument, Source


class RoutedDocumentSource:
    """`DocumentSource` that picks a delegate per source."""

    def __init__(self, local: DocumentSource, remote: DocumentSource):
        self.local = local
        self.remote = remote

    def _for(self, source: Source) -> DocumentSource:
        return self.remote if source.is_remote else self.local

    def supports(self, source: Source) -> bool:
        return self._for(source).supports(source)

    def discover(self, source: Source) -> list[str]:
        return self._for(source).discover(source)

    def fingerprint(self, source: Source, key: str) -> str:
        return self._for(source).fingerprint(source, key)

    def extract(self, source: Source, key: str) -> ExtractedDocument:
        return self._for(source).extract(source, key)

    def candidates(self, configured: set[str]) -> list[dict]:
        """Only the project tree is discoverable; a URL has to be named by a human."""
        return self.local.candidates(configured)
