"""The committed `kiln/project/knowledge.json`, read and rewritten atomically."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..domain import catalog
from ..domain.models import KnowledgeError, Source


class FileCatalogStore:
    """`CatalogStore` over a JSON file. Absent is empty, not an error."""

    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.project_root = project_root

    def sources(self) -> list[Source]:
        if not self.path.is_file():
            # A project that has not run `kiln knowledge setup` has no catalog, and launch
            # must not fail over it.
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(f"could not read knowledge catalog {self.path}: {exc}") from exc
        return catalog.parse_catalog(payload, self.project_root)

    def validate(self, source: Source) -> Source:
        return catalog.validate_source(source, self.project_root)

    def replace(self, sources: list[Source]) -> None:
        """
        Validate, then write through a temporary file.

        `os.replace` is atomic, so an interrupted write cannot leave a committed file
        half-rewritten -- this one is tracked in git and edited by a human.
        """
        validated = [catalog.validate_source(source, self.project_root) for source in sources]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = catalog.catalog_payload(validated)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
