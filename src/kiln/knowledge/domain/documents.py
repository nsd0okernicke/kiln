"""
How a note's text becomes searchable chunks.

Text in, sections out. Nothing here opens a file: reading the note and extracting its
frontmatter live in `domain.vault` and `infrastructure.vault_documents`, which hand their
*output* back through `note_sections` and `build_document`. That split is what lets the
awkward cases -- a heading with no body, a section longer than the chunk cap, an empty
note -- be tested as string transformations.
"""

from __future__ import annotations

import hashlib
import re

from .models import ExtractedDocument, KnowledgeError, Section

#: Chunks are what search ranks and what an excerpt is drawn from, so they are bounded rather
#: than one-per-heading: a 40-page section under a single heading would otherwise rank as one
#: hit and quote the wrong paragraph.
MAX_CHUNK_CHARS = 2_000


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def combined_fingerprint(fingerprints: list[str]) -> str:
    """One value standing for a whole source, so an unchanged directory is skipped wholesale."""
    return hashlib.sha256("".join(fingerprints).encode()).hexdigest()


def note_sections(content: str) -> tuple[Section, ...]:
    """Markdown sections with heading provenance."""
    return markdown_sections(content)


def markdown_sections(content: str) -> tuple[Section, ...]:
    sections: list[Section] = []
    heading = ""
    lines: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            _append_section(sections, heading, lines)
            heading = match.group(1)
            lines = []
        else:
            lines.append(line)
    _append_section(sections, heading, lines)
    # A document that is all headings and no prose still has to be indexable as something.
    return tuple(sections) or (Section("", None, content),)


def _append_section(sections: list[Section], heading: str, lines: list[str]) -> None:
    text = "\n".join(lines).strip()
    if text:
        sections.append(Section(heading, None, text))


def decode(raw: bytes, name: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KnowledgeError(f"note is not UTF-8 text: {name}") from exc


def build_document(
    *,
    relative_path: str,
    title: str,
    sections: tuple[Section, ...],
    raw: bytes,
) -> ExtractedDocument:
    """
    Assemble the indexable note, splitting oversized sections as it goes.

    `content` is joined from the *unsplit* sections so `kiln knowledge show` reproduces the
    note as written, while `sections` carries the bounded chunks search actually ranks.
    """
    return ExtractedDocument(
        relative_path=relative_path,
        title=title,
        media_type="markdown",
        content="\n\n".join(section.text for section in sections),
        fingerprint=fingerprint(raw),
        sections=tuple(chunk for section in sections for chunk in split_section(section)),
    )


def split_section(section: Section) -> tuple[Section, ...]:
    """
    Break a section at the last paragraph or word boundary before the cap.

    Falls back down a ladder -- blank line, then space, then a hard cut -- because a document
    with no paragraph breaks and no spaces still has to terminate rather than loop.
    """
    text = section.text.strip()
    if not text:
        return ()
    chunks: list[Section] = []
    while len(text) > MAX_CHUNK_CHARS:
        split_at = _split_point(text)
        chunks.append(Section(section.heading, section.page, text[:split_at].strip()))
        text = text[split_at:].strip()
    if text:
        chunks.append(Section(section.heading, section.page, text))
    return tuple(chunks)


def _split_point(text: str) -> int:
    split_at = text.rfind("\n\n", 0, MAX_CHUNK_CHARS)
    if split_at < MAX_CHUNK_CHARS // 2:
        split_at = text.rfind(" ", 0, MAX_CHUNK_CHARS)
    return split_at if split_at >= 1 else MAX_CHUNK_CHARS
