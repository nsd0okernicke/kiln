from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.knowledge.application.ports import FetchedResource
from kiln.knowledge.domain import catalog as catalog_rules
from kiln.knowledge.domain import documents, web
from kiln.knowledge.domain.models import KnowledgeError, Source
from kiln.knowledge.infrastructure.factory import build_service, index_path
from kiln.knowledge.infrastructure.file_documents import FileDocumentSource


def catalog(root: Path, sources: list[dict]) -> None:
    path = root / "kiln" / "project" / "knowledge.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "sources": sources}), encoding="utf-8")


def source(source_id: str, path: str, kind: str = "markdown") -> dict:
    return {"id": source_id, "path": path, "title": source_id.title(), "type": kind, "tags": []}


def simple_pdf(text: str) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            f"<< /Length {len(text) + 31} >>\nstream\n"
            f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\nendstream"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


class TestManifest:
    def test_rejects_duplicate_ids(self, tmp_path):
        catalog(tmp_path, [source("domain", "a.md"), source("domain", "b.md")])
        with pytest.raises(KnowledgeError, match="unique"):
            build_service(tmp_path).sources()

    @pytest.mark.parametrize(
        "entry,error",
        [
            (source("Bad ID", "docs/a.md"), "invalid"),
            (source("domain", "../outside.md"), "escapes"),
            ({**source("domain", "docs/a.md"), "type": "spreadsheet"}, "unsupported"),
        ],
    )
    def test_rejects_invalid_sources(self, tmp_path, entry, error):
        catalog(tmp_path, [entry])
        with pytest.raises(KnowledgeError, match=error):
            build_service(tmp_path).sources()

    def test_rejects_a_symlink_escape(self, tmp_path):
        outside = tmp_path.parent / "outside-knowledge.md"
        outside.write_text("secret", encoding="utf-8")
        link = tmp_path / "linked.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        catalog(tmp_path, [source("linked", "linked.md")])
        with pytest.raises(KnowledgeError, match="escapes"):
            build_service(tmp_path).sources()


class TestExtraction:
    def test_directory_rejects_a_nested_symlink_escape(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        outside = tmp_path.parent / "outside-nested-knowledge.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            (docs / "linked.txt").symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable")

        with pytest.raises(KnowledgeError, match="out-of-project link"):
            FileDocumentSource(tmp_path).discover(Source("docs", "docs", "Docs", "directory"))

    def test_markdown_preserves_headings_and_splits_large_sections(self, tmp_path):
        document = tmp_path / "guide.md"
        document.write_text("# Policy\n" + "word " * 1000, encoding="utf-8")
        extracted = FileDocumentSource(tmp_path).extract(
            Source("guide", "guide.md", "Guide", "markdown"), "guide.md"
        )
        assert len(extracted.sections) > 1
        assert {section.heading for section in extracted.sections} == {"Policy"}
        assert all(len(section.text) <= documents.MAX_CHUNK_CHARS for section in extracted.sections)

    def test_pdf_preserves_page_provenance(self, tmp_path):
        document = tmp_path / "policy.pdf"
        document.write_bytes(simple_pdf("Cancellation policy"))
        extracted = FileDocumentSource(tmp_path).extract(
            Source("policy", "policy.pdf", "Policy", "pdf"), "policy.pdf"
        )
        assert extracted.sections[0].page == 1
        assert "Cancellation policy" in extracted.content


class TestKnowledgeService:
    def test_indexes_searches_and_shows_markdown(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "domain.md").write_text(
            "# Cancellation\nSubscriptions have a fourteen day cooling-off period.",
            encoding="utf-8",
        )
        catalog(tmp_path, [source("domain", "docs/domain.md")])
        service = build_service(tmp_path)

        sync = service.sync()
        results = service.search("cancellation")
        shown = service.show(results[0].document_id)

        assert sync.updated == 1 and not sync.failures
        assert results[0].heading == "Cancellation"
        assert results[0].path == "docs/domain.md"
        assert "cooling-off" in shown["content"]

    def test_incremental_sync_updates_and_removes_documents(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        first = docs / "first.txt"
        second = docs / "second.txt"
        first.write_text("alpha", encoding="utf-8")
        second.write_text("beta", encoding="utf-8")
        catalog(tmp_path, [source("docs", "docs", "directory")])
        service = build_service(tmp_path)

        assert service.sync().updated == 2
        first.write_text("alpha changed", encoding="utf-8")
        second.unlink()
        result = service.sync()

        assert result.updated == 1
        assert result.removed == 1
        assert service.search("changed")
        assert service.search("beta") == []

    def test_failed_source_is_not_served_stale(self, tmp_path):
        document = tmp_path / "domain.txt"
        document.write_text("obsolete fact", encoding="utf-8")
        catalog(tmp_path, [source("domain", "domain.txt", "text")])
        service = build_service(tmp_path)
        service.sync()
        document.unlink()

        result = service.sync()

        assert result.failures
        assert service.search("obsolete") == []

    def test_deleted_database_is_rebuilt(self, tmp_path):
        document = tmp_path / "domain.txt"
        document.write_text("reconstructible knowledge", encoding="utf-8")
        catalog(tmp_path, [source("domain", "domain.txt", "text")])
        service = build_service(tmp_path)
        service.sync()
        index_path(tmp_path).unlink()

        service.sync()

        assert service.search("reconstructible")


HTML_PAGE = """<!doctype html>
<html><head><title>Ignored</title><style>body{color:red}</style></head>
<body>
  <nav><a href="/">Home</a></nav>
  <h1>Rate limits</h1>
  <p>Requests are capped at 100 per minute.</p>
  <script>console.log("not content")</script>
  <h2>Bursts</h2>
  <p>A burst of 20 is tolerated.</p>
</body></html>
"""


class FakeFetcher:
    """A `WebFetcher` that answers from a dict, so the url path needs no socket."""

    available = True

    def __init__(self, pages: dict[str, tuple[bytes, str]]):
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str):
        self.calls.append(url)
        if url not in self.pages:
            raise KnowledgeError(f"could not fetch {url}: not found")
        content, content_type = self.pages[url]
        return FetchedResource(url=url, content=content, content_type=content_type)


def url_source(source_id: str, url: str) -> dict:
    return {"id": source_id, "url": url, "title": source_id.title(), "type": "url", "tags": []}


class TestUrlCatalogRules:
    @pytest.mark.parametrize(
        "value,error",
        [
            ("file:///etc/passwd", "must be http"),
            ("ftp://example.com/doc.txt", "must be http"),
            ("https://", "no host"),
            ("https://user:secret@example.com/doc", "credentials"),
        ],
    )
    def test_rejects_unusable_urls(self, value, error):
        with pytest.raises(KnowledgeError, match=error):
            web.validate_url(value)

    def test_a_url_source_must_not_also_carry_a_path(self, tmp_path):
        catalog(tmp_path, [{**url_source("api", "https://example.com/api"), "path": "docs/a.md"}])
        with pytest.raises(KnowledgeError, match="must not carry a path"):
            build_service(tmp_path).sources()

    def test_a_local_source_must_not_carry_a_url(self, tmp_path):
        catalog(tmp_path, [{**source("domain", "docs/a.md"), "url": "https://example.com"}])
        with pytest.raises(KnowledgeError, match="must not carry a url"):
            build_service(tmp_path).sources()

    def test_a_url_source_requires_a_url(self, tmp_path):
        entry = url_source("api", "https://example.com")
        del entry["url"]
        catalog(tmp_path, [entry])
        with pytest.raises(KnowledgeError, match="requires a url"):
            build_service(tmp_path).sources()

    def test_a_local_catalog_round_trips_without_gaining_a_url_field(self, tmp_path):
        """Adding the field must not rewrite every existing project's catalog."""
        entry = catalog_rules.source_dict(Source("domain", "docs/a.md", "Domain", "markdown"))
        assert "url" not in entry
        assert catalog_rules.source_dict(
            Source("api", "", "Api", "url", (), "https://example.com/doc")
        ).keys() == {"id", "title", "type", "tags", "url"}


class TestHtmlExtraction:
    def test_headings_become_sections_and_chrome_is_dropped(self):
        sections = web.html_sections(HTML_PAGE)
        headings = [section.heading for section in sections]

        assert "Rate limits" in headings
        assert "Bursts" in headings
        joined = "\n".join(section.text for section in sections)
        assert "capped at 100 per minute" in joined
        assert "not content" not in joined, "script bodies are not prose"
        assert "color:red" not in joined, "style bodies are not prose"

    def test_an_unsupported_content_type_is_named_rather_than_indexed(self):
        with pytest.raises(KnowledgeError, match="unsupported knowledge content type"):
            web.media_type_for("application/zip")

    def test_a_title_is_derived_from_the_url_when_none_is_given(self):
        assert web.default_title("https://example.com/docs/rate-limits.html") == "Rate Limits"
        assert web.default_title("https://example.com") == "example.com"


class TestUrlIndexing:
    def _service(self, tmp_path, pages, **kwargs):
        return build_service(tmp_path, fetcher=FakeFetcher(pages), **kwargs)

    def test_indexes_and_searches_a_remote_page(self, tmp_path):
        catalog(tmp_path, [url_source("api", "https://example.com/limits")])
        pages = {"https://example.com/limits": (HTML_PAGE.encode(), "text/html; charset=utf-8")}
        service = self._service(tmp_path, pages)

        result = service.sync()
        found = service.search("burst tolerated")

        assert result.updated == 1 and not result.failures
        assert found[0].path == "https://example.com/limits", "a citation must name the url"
        assert found[0].heading == "Bursts"

    def test_an_unchanged_page_is_skipped_on_the_next_sync(self, tmp_path):
        catalog(tmp_path, [url_source("api", "https://example.com/limits")])
        pages = {"https://example.com/limits": (HTML_PAGE.encode(), "text/html")}
        service = self._service(tmp_path, pages)
        service.sync()

        assert self._service(tmp_path, pages).sync().skipped == 1

    def test_one_sync_fetches_a_url_once(self, tmp_path):
        """Fingerprint then extract is two asks; it must not be two requests."""
        catalog(tmp_path, [url_source("api", "https://example.com/limits")])
        fetcher = FakeFetcher({"https://example.com/limits": (HTML_PAGE.encode(), "text/html")})

        build_service(tmp_path, fetcher=fetcher).sync()

        assert fetcher.calls == ["https://example.com/limits"]

    def test_an_unreachable_url_fails_that_source_only(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "local.md").write_text("# Local\nstill indexed", encoding="utf-8")
        catalog(
            tmp_path,
            [source("docs", "docs/local.md"), url_source("api", "https://example.com/gone")],
        )
        service = self._service(tmp_path, {})

        result = service.sync()

        assert len(result.failures) == 1 and "api" in result.failures[0]
        assert result.updated == 1, "the local source still indexed"
        assert service.search("still indexed")

    def test_offline_defers_a_url_and_keeps_what_it_already_indexed(self, tmp_path):
        """
        Deferred, not failed. Failing a source drops its documents, so treating "the network
        is off" as "this source is broken" would empty the index on a train and force a full
        refetch afterwards.
        """
        catalog(tmp_path, [url_source("api", "https://example.com/limits")])
        pages = {"https://example.com/limits": (HTML_PAGE.encode(), "text/html")}
        self._service(tmp_path, pages).sync()

        result = build_service(tmp_path, offline=True).sync()

        assert result.deferred == ["api"]
        assert not result.failures
        assert build_service(tmp_path, offline=True).search("burst tolerated"), (
            "the previously indexed page is still searchable offline"
        )


class TestPdfEdgeCases:
    def test_a_page_with_no_extractable_text_is_dropped_not_indexed_empty(self, tmp_path):
        """Scanned or decorative pages produce nothing; an empty chunk would rank as a hit."""
        document = tmp_path / "mixed.pdf"
        document.write_bytes(simple_pdf(" "))

        extracted = FileDocumentSource(tmp_path).extract(
            Source("mixed", "mixed.pdf", "Mixed", "pdf"), "mixed.pdf"
        )

        assert extracted.sections == ()

    def test_an_unreadable_pdf_is_reported_as_a_knowledge_error(self, tmp_path):
        """`sync` records `KnowledgeError`; anything else would escape and stop the run."""
        document = tmp_path / "broken.pdf"
        document.write_bytes(b"%PDF-1.4\nnot really a pdf")

        with pytest.raises(KnowledgeError, match="could not extract PDF"):
            FileDocumentSource(tmp_path).extract(
                Source("broken", "broken.pdf", "Broken", "pdf"), "broken.pdf"
            )


class TestCandidateDiscovery:
    def test_a_project_without_docs_or_readme_offers_nothing(self, tmp_path):
        assert FileDocumentSource(tmp_path).candidates(set()) == []

    def test_directories_and_unsupported_files_are_not_offered(self, tmp_path):
        docs = tmp_path / "docs"
        (docs / "nested").mkdir(parents=True)
        (docs / "guide.md").write_text("# Guide", encoding="utf-8")
        (docs / "diagram.png").write_bytes(b"\x89PNG")
        (docs / "nested" / "deep.txt").write_text("deep", encoding="utf-8")

        found = FileDocumentSource(tmp_path).candidates(set())

        assert [item["path"] for item in found] == ["docs/guide.md", "docs/nested/deep.txt"]

    def test_an_already_catalogued_file_is_not_offered_again(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide", encoding="utf-8")

        assert FileDocumentSource(tmp_path).candidates({"docs/guide.md"}) == []

    def test_a_readme_at_the_project_root_is_offered(self, tmp_path):
        (tmp_path / "README.md").write_text("# Project", encoding="utf-8")

        assert FileDocumentSource(tmp_path).candidates(set()) == [
            {"path": "README.md", "type": "markdown"}
        ]
