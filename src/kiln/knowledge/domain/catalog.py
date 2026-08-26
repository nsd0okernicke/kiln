"""
Rules the committed knowledge catalog must satisfy, independent of where it is stored.

Pure by design: parsing takes a decoded payload rather than a path, so every rejection the
acceptance criteria name -- duplicate ids, unsupported types, absolute or out-of-project paths,
symlink escapes -- is testable without writing a file. Reading and writing `knowledge.json`
belongs to `infrastructure.catalog_file`.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from . import web
from .models import LOCAL_TYPES, URL_TYPE, KnowledgeError, Source

VERSION = 1
SOURCE_TYPES = {*LOCAL_TYPES, URL_TYPE}
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def empty_catalog() -> dict:
    return {"version": VERSION, "sources": []}


def parse_catalog(payload: object, project_root: Path) -> list[Source]:
    """Every source in a decoded catalog document, or `KnowledgeError` naming the first fault."""
    sources = [_parse_source(item, project_root) for item in _source_entries(payload)]
    _require_unique_ids(sources)
    return sources


def catalog_payload(sources: list[Source]) -> dict:
    return {"version": VERSION, "sources": [source_dict(source) for source in sources]}


def source_dict(source: Source) -> dict:
    """
    A catalog entry.

    The locator field that does not apply is dropped rather than written empty, so a catalog of
    local sources round-trips byte-for-byte through a Kiln that understands urls.
    """
    result = asdict(source)
    result["tags"] = list(source.tags)
    result.pop("url" if not source.is_remote else "path", None)
    return result


def validate_source(source: Source, project_root: Path) -> Source:
    """Put a caller-built source through the same gate a catalog entry goes through."""
    return _parse_source(source_dict(source), project_root)


def validate_source_path(project_root: Path, value: str) -> Path:
    """
    Resolve a catalog path, refusing anything that leaves the project.

    Both halves matter and neither implies the other: an absolute path is rejected outright,
    and a relative one is resolved before the containment check so that `../` and symlinks
    cannot walk out of the tree.
    """
    candidate = Path(value)
    if candidate.is_absolute():
        raise KnowledgeError(f"knowledge source path must be project-relative: {value}")
    root = project_root.resolve()
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise KnowledgeError(f"knowledge source escapes the project: {value}") from exc
    return resolved


def _parse_source(item: object, project_root: Path) -> Source:
    if not isinstance(item, dict):
        raise KnowledgeError("each knowledge source must be an object")
    source_id = _required_string(item, "id")
    if not SOURCE_ID.fullmatch(source_id):
        raise KnowledgeError(f"invalid knowledge source id: {source_id!r}")
    source_type = item.get("type")
    if source_type not in SOURCE_TYPES:
        raise KnowledgeError(f"source {source_id!r} has unsupported type {source_type!r}")
    title = _required_string(item, "title", source_id)
    tags = _tags(item.get("tags", []), source_id)
    path, url = _locator(item, source_id, str(source_type), project_root)
    return Source(source_id, path, title, str(source_type), tags, url)


def _locator(item: dict, source_id: str, source_type: str, project_root: Path) -> tuple[str, str]:
    """A source is addressed by a contained path or by an outward url, never by both."""
    if source_type == URL_TYPE:
        if item.get("path"):
            raise KnowledgeError(f"source {source_id!r} is a url and must not carry a path")
        return "", web.validate_url(_required_string(item, "url", source_id))
    if item.get("url"):
        raise KnowledgeError(f"source {source_id!r} is a {source_type} and must not carry a url")
    source_path = _required_string(item, "path", source_id)
    validate_source_path(project_root, source_path)
    return source_path, ""


def _source_entries(payload: object) -> list:
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        raise KnowledgeError(f"knowledge catalog must have version {VERSION}")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise KnowledgeError("knowledge catalog sources must be a list")
    return sources


def _require_unique_ids(sources: list[Source]) -> None:
    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise KnowledgeError("knowledge source ids must be unique")


def _required_string(item: dict, field: str, source_id: str = "") -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        prefix = f"source {source_id!r} " if source_id else "knowledge source "
        raise KnowledgeError(f"{prefix}requires a {field}")
    return value.strip()


def _tags(value: object, source_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(tag, str) and tag for tag in value):
        raise KnowledgeError(f"source {source_id!r} tags must be non-empty strings")
    return tuple(value)
