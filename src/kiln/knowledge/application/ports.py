"""
What the knowledge use cases need from the outside world.

Three protocols rather than one gateway, because the three are independently substitutable and
have genuinely different lifetimes: the catalog is a committed file, the documents are whatever
the project happens to have on disk, and the index is a disposable SQLite database that can be
deleted between calls.

`KnowledgeIndex` owns its own transactions on purpose. The service used to open a connection,
commit after each source and roll back inside its own error handler, which put SQLite's unit of
work in the layer least able to reason about it -- and made "what happens to a half-indexed
source" a question about the use case rather than about the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.models import ExtractedDocument, SearchResult, Source


class CatalogStore(Protocol):
    """The committed `kiln/project/knowledge.json`."""

    def sources(self) -> list[Source]: ...

    def replace(self, sources: list[Source]) -> None: ...

    #: Put a caller-built source through the catalog's own rules. Separate from `replace` so a
    #: bad path is rejected before the duplicate-id check reports something less useful.
    def validate(self, source: Source) -> Source: ...


class DocumentSource(Protocol):
    """
    The documents a catalog source resolves to.

    Addressed by an opaque string key rather than a `Path`. A local source's key is its
    project-relative path and a remote one's is its URL, and the use case needs the key only to
    compare fingerprints and to record what it indexed -- so making it a path would have forced
    every remote source to pretend to be a file.
    """

    def supports(self, source: Source) -> bool:
        """
        Whether this source can be read right now.

        False means *leave it alone* -- not indexed, and not touched. Offline is the case that
        matters: a remote source that cannot be fetched must keep the documents it already has
        rather than being treated as broken and emptied.
        """
        ...

    def discover(self, source: Source) -> list[str]: ...

    def fingerprint(self, source: Source, key: str) -> str: ...

    def extract(self, source: Source, key: str) -> ExtractedDocument: ...

    def candidates(self, configured: set[str]) -> list[dict]: ...


@dataclass(frozen=True)
class FetchedResource:
    """One retrieved remote document, as bytes plus the header that says how to read them."""

    url: str
    content: bytes
    content_type: str


class WebFetcher(Protocol):
    """
    Retrieval of a remote source.

    A port rather than a direct `urllib` call inside the adapter, so the whole url path -- the
    catalog rules, the HTML reduction, the indexing -- is exercised by the test suite without a
    socket, a server or a network-dependent build.
    """

    #: False when fetching is disabled, so remote sources are deferred rather than failed.
    available: bool

    def fetch(self, url: str) -> FetchedResource: ...


class KnowledgeIndex(Protocol):
    """
    The disposable search index.

    Every method is complete in itself -- it either lands durably or leaves the index as it
    was. `mark_failed` in particular discards whatever the interrupted source had written and
    records why, so a failing source can never be served as stale results.
    """

    def ready(self) -> bool: ...

    def start_run(self) -> int: ...

    def finish_run(self, run_id: int, counts: dict[str, int]) -> None: ...

    def drop_sources_except(self, retained_ids: set[str]) -> int: ...

    def mark_indexing(self, source: Source) -> None: ...

    def mark_indexed(self, source: Source, fingerprint: str) -> None: ...

    def mark_failed(self, source: Source, error: str) -> int: ...

    def known_fingerprint(self, source_id: str, path: str) -> str | None: ...

    def replace_document(self, source: Source, document: ExtractedDocument) -> None: ...

    def drop_documents_except(self, source_id: str, retained: set[str]) -> int: ...

    def search(self, query: str, limit: int) -> list[SearchResult]: ...

    def document(self, identifier: str) -> dict: ...

    def close(self) -> None: ...
