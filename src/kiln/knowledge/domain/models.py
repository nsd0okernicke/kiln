"""Knowledge catalog and search value objects."""

from __future__ import annotations

from dataclasses import dataclass, field


class KnowledgeError(Exception):
    """A catalog, extraction, or search operation could not be completed."""


#: Catalog types resolved from the project tree. `url` is the one that is not.
LOCAL_TYPES = ("markdown", "text", "pdf", "directory")
URL_TYPE = "url"


@dataclass(frozen=True)
class Source:
    """
    One catalogued origin: a project-relative path, or -- for `type: "url"` -- a remote address.

    `path` and `url` are exclusive rather than one field doing both jobs. They are validated by
    opposite rules (a path must stay inside the project; a URL must leave it), and a single
    field would make every read of a catalog entry ask which kind it was looking at.
    """

    id: str
    path: str
    title: str
    type: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    url: str = ""

    @property
    def is_remote(self) -> bool:
        return self.type == URL_TYPE

    @property
    def locator(self) -> str:
        """What this source is addressed by, for messages and for the indexed document path."""
        return self.url if self.is_remote else self.path


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
