"""
What the knowledge use cases need from the outside world.

Two protocols rather than one gateway, because the two have genuinely different lifetimes:
the documents are whatever the configured vault happens to have on disk, and the index is a
disposable SQLite database that can be deleted between calls. The config is a plain value
object parsed in `domain.vault` -- a protocol would give a file-shaped thing nothing to be.

`KnowledgeIndex` owns its own transactions on purpose. The service used to open a connection,
commit after each source and roll back inside its own error handler, which put SQLite's unit of
work in the layer least able to reason about it -- and made "what happens to a half-indexed
source" a question about the use case rather than about the store.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.models import ExtractedDocument, SearchResult, Source


class DocumentSource(Protocol):
    """
    The notes of the configured Obsidian vault.

    Addressed by an opaque string key rather than a `Path`. A note's key is its vault-relative
    path and the use case needs the key only to compare fingerprints and to record what it
    indexed -- so making it a path would have forced the protocol to pretend to know a
    filesystem.
    """

    def source(self) -> Source:
        """The single indexed origin the notes belong to."""
        ...

    def supports(self) -> bool:
        """
        Whether the vault can be read right now.

        False means *leave it alone* -- not indexed, and not touched. A vault on a network
        drive that is not mounted is the case that matters: it must keep the documents it
        already has rather than being treated as broken and emptied.
        """
        ...

    def discover(self) -> list[str]: ...

    def fingerprint(self, key: str) -> str: ...

    def extract(self, key: str) -> ExtractedDocument: ...

    def related(self, key: str) -> list[str]:
        """The existing notes `key` links to, resolved. An addition to `show`, never the answer."""
        ...


class KnowledgeIndex(Protocol):
    """
    The disposable search index.

    Every method is complete in itself -- it either lands durably or leaves the index as it
    was. `mark_failed` in particular discards whatever the interrupted sync had written and
    records why, so a failing sync can never be served as stale results.
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

    def overview(self) -> dict:
        """The status payload -- newest sync run, the source row, the document count."""
        ...

    def close(self) -> None: ...
