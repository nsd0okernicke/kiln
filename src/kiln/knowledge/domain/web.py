"""
Rules for remote knowledge sources: what a usable URL is, and how a response becomes text.

Pure. Nothing here opens a socket -- fetching lives in `infrastructure.http_fetcher`, so the
awkward parts (a URL with credentials in it, an HTML page full of script tags, a response whose
content type disagrees with its extension) are testable without a server.

HTML is reduced with the standard library rather than a parser dependency. The goal is
retrievable prose, not fidelity: script, style and navigation chrome are dropped, block
elements become line breaks, and headings become section boundaries the same way markdown ones
do -- so a citation can name the heading it came from.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

from .models import KnowledgeError, Section

#: Only these. `file://` would turn a knowledge source into an arbitrary local-file read that
#: bypasses the project-containment rule every path source is held to, and `ftp://`/`data:`
#: buy nothing a curated documentation catalog needs.
ALLOWED_SCHEMES = ("http", "https")

#: Response content types this version can turn into text, mapped to the media type recorded
#: on the document. Anything else is refused by name rather than indexed as bytes.
CONTENT_TYPES = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/plain": "text",
    "application/pdf": "pdf",
}

#: Elements whose text is chrome, not content.
IGNORED_ELEMENTS = frozenset({"script", "style", "noscript", "template", "svg"})

#: Elements that end a line of prose.
BLOCK_ELEMENTS = frozenset(
    {"p", "div", "section", "article", "li", "tr", "br", "pre", "blockquote", "table"}
)

HEADING_ELEMENTS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def validate_url(value: str) -> str:
    """A catalog URL, or `KnowledgeError` saying which rule it broke."""
    parsed = urlparse(value)
    if parsed.scheme not in ALLOWED_SCHEMES:
        allowed = " or ".join(ALLOWED_SCHEMES)
        raise KnowledgeError(f"knowledge source url must be {allowed}: {value}")
    if not parsed.hostname:
        raise KnowledgeError(f"knowledge source url has no host: {value}")
    if parsed.username or parsed.password:
        # Credentials would be written into a committed catalog file.
        raise KnowledgeError(f"knowledge source url must not carry credentials: {value}")
    return value


def looks_like_url(value: str) -> bool:
    """Whether a `kiln knowledge add` argument is a URL rather than a path."""
    return urlparse(value).scheme in ALLOWED_SCHEMES


def media_type_for(content_type: str) -> str:
    """The media type for a response, from its `Content-Type` header alone."""
    kind = CONTENT_TYPES.get(content_type.split(";")[0].strip().lower())
    if kind is None:
        raise KnowledgeError(f"unsupported knowledge content type: {content_type}")
    return kind


def default_title(url: str) -> str:
    """A readable fallback when the catalog entry names no title."""
    parsed = urlparse(url)
    last = [part for part in parsed.path.split("/") if part]
    if not last:
        return parsed.netloc
    return re.sub(r"[-_]+", " ", last[-1].rsplit(".", 1)[0]).strip().title() or parsed.netloc


def html_sections(markup: str) -> tuple[Section, ...]:
    """Readable prose from an HTML document, split at its headings."""
    reader = _HtmlText()
    reader.feed(markup)
    reader.close()
    return reader.sections()


class _HtmlText(HTMLParser):
    """Collects text per heading, skipping elements that never carry content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._sections: list[Section] = []
        self._heading = ""
        self._lines: list[str] = []
        self._ignoring = 0
        self._in_heading = False
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in IGNORED_ELEMENTS:
            self._ignoring += 1
        elif tag in HEADING_ELEMENTS:
            self._close_section()
            self._in_heading = True
            self._heading_parts = []
        elif tag in BLOCK_ELEMENTS:
            self._lines.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORED_ELEMENTS:
            self._ignoring = max(self._ignoring - 1, 0)
        elif tag in HEADING_ELEMENTS and self._in_heading:
            self._heading = _collapse(" ".join(self._heading_parts))
            self._in_heading = False

    def handle_data(self, data: str) -> None:
        if self._ignoring:
            return
        if self._in_heading:
            self._heading_parts.append(data)
        else:
            self._lines.append(data)

    def _close_section(self) -> None:
        text = _collapse_block("".join(_joined(self._lines)))
        if text:
            self._sections.append(Section(self._heading, None, text))
        self._lines = []

    def sections(self) -> tuple[Section, ...]:
        self._close_section()
        return tuple(self._sections)


def _joined(lines: list[str]) -> list[str]:
    return [line if line else "\n" for line in lines]


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _collapse_block(value: str) -> str:
    """Squeeze runs of spaces and blank lines without losing paragraph boundaries."""
    lines = [_collapse(line) for line in unescape(value).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
