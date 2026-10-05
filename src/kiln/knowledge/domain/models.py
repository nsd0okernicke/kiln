"""Knowledge search value objects."""

from __future__ import annotations

from dataclasses import dataclass, field


class KnowledgeError(Exception):
    """A config, extraction, or search operation could not be completed."""


@dataclass(frozen=True)
class Source:
    """
    The one indexed origin: the configured Obsidian vault.

    One vault is one source and the per-note rows live in `documents`; the fields are what
    the index's `sources` table stores and what a citation names.
    """

    id: str
    path: str
    title: str
    type: str
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Section:
    heading: str
    page: int | None
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    relative_path: str
    title: str
    media_type: str
    content: str
    fingerprint: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    document_id: str
    source_id: str
    source_title: str
    path: str
    heading: str
    page: int | None
    excerpt: str
    indexed_at: str
    freshness: str
    rank: float
