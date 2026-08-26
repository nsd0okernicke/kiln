"""
Where the concrete adapters are bound to the use cases.

The one place that knows both that a catalog is a file at `kiln/project/knowledge.json` and
that the index is SQLite at `.kiln/knowledge.db` -- the committed half and the disposable half,
which is why the two paths are stated together here rather than inside either adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..application.knowledge_service import KnowledgeService
from ..application.ports import WebFetcher
from .catalog_file import FileCatalogStore
from .file_documents import FileDocumentSource
from .http_fetcher import OfflineFetcher, UrllibFetcher
from .routed_documents import RoutedDocumentSource
from .sqlite_index import SqliteKnowledgeIndex
from .web_documents import WebDocumentSource

CATALOG_RELATIVE_PATH = Path("kiln") / "project" / "knowledge.json"
INDEX_RELATIVE_PATH = Path(".kiln") / "knowledge.db"


def catalog_path(project_root: Path) -> Path:
    return project_root / CATALOG_RELATIVE_PATH


def index_path(project_root: Path) -> Path:
    return project_root / INDEX_RELATIVE_PATH


def build_service(
    project_root: Path, *, offline: bool = False, fetcher: WebFetcher | None = None
) -> KnowledgeService:
    """
    A service wired to the real filesystem, network and database. The caller closes it.

    `offline` swaps in a fetcher that refuses rather than dropping url sources from the run:
    a source nobody was told was skipped is worse than one reported as failed.
    """
    root = project_root.resolve()
    remote = fetcher or (OfflineFetcher() if offline else UrllibFetcher())
    return KnowledgeService(
        catalog=FileCatalogStore(catalog_path(root), root),
        documents=RoutedDocumentSource(
            local=FileDocumentSource(root), remote=WebDocumentSource(remote)
        ),
        index=SqliteKnowledgeIndex(index_path(root)),
    )


@contextmanager
def knowledge_service(
    project_root: Path, *, offline: bool = False, fetcher: WebFetcher | None = None
) -> Iterator[KnowledgeService]:
    """`build_service` with the database connection closed on the way out."""
    service = build_service(project_root, offline=offline, fetcher=fetcher)
    try:
        yield service
    finally:
        service.index.close()
