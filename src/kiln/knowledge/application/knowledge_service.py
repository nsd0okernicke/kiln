"""
Vault knowledge lifecycle and incremental indexing.

Depends on `ports` and `domain` and on nothing concrete: no sqlite3, no argparse, no path
literals. `infrastructure.factory` is what binds the real vault config, the real vault reader
and the real SQLite index to it, which is also what lets these use cases be exercised against
fakes.

Incremental by fingerprint, per note. A note is re-read only where its content changed, so a
sync over an unchanged vault is a series of hash comparisons rather than a re-index.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..domain import documents
from ..domain.models import KnowledgeError, SearchResult, Source
from ..domain.vault import VaultConfig
from .ports import DocumentSource, KnowledgeIndex

NOT_INDEXED = "knowledge index not found; run `kiln knowledge sync`"
VAULT_NOT_CONFIGURED = (
    'knowledge vault is not configured; set "vault" in kiln/project/knowledge.json'
)


@dataclass
class SyncResult:
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failures: list[str] = field(default_factory=list)
    #: The vault left untouched because nothing could read it this run (not mounted).
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

    def __init__(
        self,
        config: VaultConfig | None,
        vault_documents: DocumentSource | None,
        index: KnowledgeIndex,
    ):
        self.config = config
        self.documents = vault_documents
        self.index = index

    # -- status, what the CLI and the cockpit both show --------------------------------------

    def status(self) -> dict:
        """
        The configuration plus the last sync, without searching.

        The index is asked with `overview()`, which is side-effect free: a project that has
        never synced is "never synced", not a database created by asking about one.
        """
        fallback = self.config.path if self.config else ""
        return {
            "configured": self.documents is not None,
            "vault": self.documents.root.as_posix() if self.documents else fallback,
            "exclude": list(self.config.exclude) if self.config else [],
            **self.index.overview(),
        }

    # -- indexing --------------------------------------------------------------------------

    def sync(self) -> SyncResult:
        """
        Bring the index in line with the vault. Never raises for a single bad note.

        A vault that cannot be read is recorded as deferred and its documents stay, while a
        note that cannot be read marks the source failed -- the acceptance criteria want a
        nonzero exit from the *command*, not an abandoned run.
        """
        if self.documents is None:
            raise KnowledgeError(VAULT_NOT_CONFIGURED)
        result = SyncResult()
        run_id = self.index.start_run()
        try:
            if not self.documents.supports():
                # Untouched on purpose: its indexed notes stay searchable and stay attributed
                # to it, and the caller is told which origin went unrefreshed.
                result.deferred.append(self.documents.source().id)
            else:
                self._sync_vault(result)
        finally:
            self.index.finish_run(run_id, result.counts())
        return result

    def _sync_vault(self, result: SyncResult) -> None:
        assert self.documents is not None  # sync guards the unconfigured case before this
        source = self.documents.source()
        try:
            # One vault is the only source now; this also clears rows a pre-vault sync left.
            result.removed += self.index.drop_sources_except({source.id})
            counts = self._index_notes(source)
        except (KnowledgeError, OSError) as exc:
            result.removed += self.index.mark_failed(source, str(exc))
            result.failures.append(f"{source.id}: {exc}")
            return
        result.updated += counts["updated"]
        result.skipped += counts["skipped"]
        result.removed += counts["removed"]

    def _index_notes(self, source: Source) -> dict[str, int]:
        assert self.documents is not None  # _sync_vault narrows before this
        self.index.mark_indexing(source)
        retained: set[str] = set()
        fingerprints: list[str] = []
        updated = 0
        skipped = 0
        for key in self.documents.discover():
            retained.add(key)
            fingerprint = self.documents.fingerprint(key)
            fingerprints.append(fingerprint)
            if self.index.known_fingerprint(source.id, key) == fingerprint:
                skipped += 1
                continue
            self.index.replace_document(source, self.documents.extract(key))
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
        document = self.index.document(identifier)
        document["related"] = self._related(document)
        return document

    def _related(self, document: dict) -> list[str]:
        if self.documents is None:
            return []
        try:
            return self.documents.related(document["path"])
        except (KnowledgeError, OSError):
            # Related notes are an addition to the answer, never the answer itself.
            return []

    def _require_index(self) -> None:
        # Deleting `.kiln/knowledge.db` is supported, so "no index" is an ordinary state with
        # an instruction attached rather than an error about a missing file.
        if not self.index.ready():
            raise KnowledgeError(NOT_INDEXED)


def result_dict(result: SearchResult) -> dict:
    return asdict(result)
