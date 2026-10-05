"""
Where the concrete adapters are bound to the use cases.

The one place that knows both that the vault config is a file at `kiln/project/knowledge.json`
(committed) and that the index is SQLite at `.kiln/knowledge.db` (disposable) -- the committed
half and the disposable half, which is why the two paths are stated together here rather than
inside either adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..application.knowledge_service import KnowledgeService
from ..domain import vault
from ..domain.models import KnowledgeError
from ..domain.vault import VaultConfig
from .sqlite_index import SqliteKnowledgeIndex
from .vault_documents import VaultDocumentSource

CONFIG_RELATIVE_PATH = Path("kiln") / "project" / "knowledge.json"
INDEX_RELATIVE_PATH = Path(".kiln") / "knowledge.db"


def config_path(project_root: Path) -> Path:
    return project_root / CONFIG_RELATIVE_PATH


def index_path(project_root: Path) -> Path:
    return project_root / INDEX_RELATIVE_PATH


def read_config(project_root: Path) -> VaultConfig | None:
    """
    The committed vault config, or None when the project has none yet.

    Absent is "not configured", not an error -- a project that never points at a vault must
    launch, sync and search without one. Unreadable or malformed is an error: a config with
    a typo in it must be seen, not silently ignored.
    """
    path = config_path(project_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError(f"could not read knowledge config {path}: {exc}") from exc
    return vault.parse_config(payload)


def build_service(project_root: Path) -> KnowledgeService:
    """A service wired to the real filesystem and database. The caller closes it."""
    root = project_root.resolve()
    config = read_config(root)
    documents = (
        VaultDocumentSource(config, root) if config is not None and config.configured else None
    )
    return KnowledgeService(
        config=config,
        vault_documents=documents,
        index=SqliteKnowledgeIndex(index_path(root)),
    )


@contextmanager
def knowledge_service(project_root: Path) -> Iterator[KnowledgeService]:
    """`build_service` with the database connection closed on the way out."""
    service = build_service(project_root)
    try:
        yield service
    finally:
        service.index.close()
