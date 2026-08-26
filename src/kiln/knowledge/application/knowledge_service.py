"""
Knowledge catalog lifecycle and incremental indexing (issue #3).

Depends on `ports` and `domain` and on nothing concrete: no sqlite3, no argparse, no path
literals. `infrastructure.factory` is what binds the real file catalog, the real extractor and
the real SQLite index to it, which is also what lets these use cases be exercised against fakes.

Incremental by fingerprint, per file. A source is re-read only where its content changed, so a
sync over an unchanged docs directory is a series of hash comparisons rather than a re-index.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..domain import documents
from ..domain.models import KnowledgeError, SearchResult, Source
from .ports import CatalogStore, DocumentSource, KnowledgeIndex

NOT_INDEXED = "knowledge index not found; run `kiln knowledge sync`"


@dataclass
class SyncResult:
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failures: list[str] = field(default_factory=list)
    #: Sources left untouched because nothing could read them this run (offline urls).
    deferred: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "updated": self.updated,
            "skipped": self.skipped,
            "removed": self.removed,
            "failed": len(self.failures),
        }

    def as_dict(self) -> dict:
        return {**self.counts(), "deferred": self.deferred, "failures": self.failures}


class KnowledgeService:
    """Use cases behind `kiln knowledge`."""

    def __init__(self, catalog: CatalogStore, documents: DocumentSource, index: KnowledgeIndex):
        self.catalog = catalog
        self.documents = documents
        self.index = index

    # -- catalog curation, the human-in-the-loop's half -------------------------------------

    def sources(self) -> list[Source]:
        return self.catalog.sources()

    def add(self, source: Source) -> None:
        source = self.catalog.validate(source)
        sources = self.sources()
        if any(existing.id == source.id for existing in sources):
            raise KnowledgeError(f"knowledge source already exists: {source.id}")
        self.catalog.replace([*sources, source])

    def remove(self, source_id: str) -> None:
        sources = self.sources()
        retained = [source for source in sources if source.id != source_id]
        if len(retained) == len(sources):
            raise KnowledgeError(f"knowledge source not found: {source_id}")
        self.catalog.replace(retained)

    def candidates(self) -> list[dict]:
        return self.documents.candidates({source.path for source in self.sources()})

    # -- indexing --------------------------------------------------------------------------

    def sync(self) -> SyncResult:
        """
        Bring the index in line with the catalog. Never raises for a single bad source.

        A source that cannot be read is recorded as failed and its documents dropped, while the
        rest of the catalog still indexes -- the acceptance criteria want a nonzero exit from
        the *command*, not an abandoned run.
        """
        sources = self.sources()
        result = SyncResult()
        run_id = self.index.start_run()
        result.removed += self.index.drop_sources_except({source.id for source in sources})
        for source in sources:
            if not self.documents.supports(source):
                # Untouched on purpose: its indexed documents stay searchable and stay
                # attributed to it, and the caller is told which sources went unrefreshed.
                result.deferred.append(source.id)
                continue
            self._sync_source(source, result)
        self.index.finish_run(run_id, result.counts())
        return result

    def _sync_source(self, source: Source, result: SyncResult) -> None:
        try:
            counts = self._index_source(source)
        except (KnowledgeError, OSError) as exc:
            result.removed += self.index.mark_failed(source, str(exc))
            result.failures.append(f"{source.id}: {exc}")
            return
        result.updated += counts["updated"]
        result.skipped += counts["skipped"]
        result.removed += counts["removed"]

    def _index_source(self, source: Source) -> dict[str, int]:
        self.index.mark_indexing(source)
        retained: set[str] = set()
        fingerprints: list[str] = []
        updated = 0
        skipped = 0
        for key in self.documents.discover(source):
            retained.add(key)
            fingerprint = self.documents.fingerprint(source, key)
            fingerprints.append(fingerprint)
            if self.index.known_fingerprint(source.id, key) == fingerprint:
                skipped += 1
                continue
            self.index.replace_document(source, self.documents.extract(source, key))
            updated += 1
        removed = self.index.drop_documents_except(source.id, retained)
        self.index.mark_indexed(source, documents.combined_fingerprint(fingerprints))
        return {"updated": updated, "skipped": skipped, "removed": removed}

    # -- retrieval, what every role is allowed to do ----------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self._require_index()
        return self.index.search(query, limit)

    def show(self, identifier: str) -> dict:
        self._require_index()
        return self.index.document(identifier)

    def _require_index(self) -> None:
        # Deleting `.kiln/knowledge.db` is supported, so "no index" is an ordinary state with
        # an instruction attached rather than an error about a missing file.
        if not self.index.ready():
            raise KnowledgeError(NOT_INDEXED)


def result_dict(result: SearchResult) -> dict:
    return asdict(result)
