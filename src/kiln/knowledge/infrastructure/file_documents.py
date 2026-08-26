"""
Local files behind a catalog source: finding them, reading them, extracting PDF pages.

Everything that needs the disk. The text rules it applies -- what a supported file is, how
markdown splits on headings, how an oversized section is chunked -- live in `domain.documents`
and are called from here rather than reimplemented.
"""

from __future__ import annotations

from pathlib import Path

from ..domain import documents
from ..domain.models import ExtractedDocument, KnowledgeError, Section, Source

#: Where `kiln knowledge setup` looks for documentation nobody has catalogued yet.
CANDIDATE_ROOTS = ("docs", "README.md")


class FileDocumentSource:
    """`DocumentSource` over the project tree."""

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def supports(self, source: Source) -> bool:
        """The project tree is always readable; a missing file is a failure, not a deferral."""
        return True

    def discover(self, source: Source) -> list[str]:
        target = (self.project_root / source.path).resolve(strict=False)
        if not target.exists():
            raise KnowledgeError(f"source not found: {source.path}")
        if target.is_file():
            documents.require_supported_file(target.name, target.suffix, source.type)
            return [self.relative_path(target)]
        if source.type != "directory":
            raise KnowledgeError(f"source {source.id!r} is a directory but type is {source.type!r}")
        files = self._supported_files(target)
        self._reject_escaped_files(files)
        keys = [self.relative_path(path) for path in files]
        return sorted(keys, key=str.lower)

    def relative_path(self, path: Path) -> str:
        return path.relative_to(self.project_root).as_posix()

    def _resolve(self, key: str) -> Path:
        """A discovered key back to the file it names. Re-checked, never trusted blindly."""
        path = (self.project_root / key).resolve(strict=False)
        try:
            path.relative_to(self.project_root.resolve())
        except ValueError as exc:
            raise KnowledgeError(f"knowledge source escapes the project: {key}") from exc
        return path

    def fingerprint(self, source: Source, key: str) -> str:
        return documents.fingerprint(self._resolve(key).read_bytes())

    def extract(self, source: Source, key: str) -> ExtractedDocument:
        path = self._resolve(key)
        raw = path.read_bytes()
        kind = documents.media_type(path.suffix)
        sections = self._sections(path, raw, kind)
        return documents.build_document(
            relative_path=key,
            title=path.stem,
            source=source,
            kind=kind,
            sections=sections,
            raw=raw,
        )

    def candidates(self, configured: set[str]) -> list[dict]:
        found: list[dict] = []
        for name in CANDIDATE_ROOTS:
            for path in self._candidate_paths(self.project_root / name):
                relative = self.relative_path(path)
                if relative in configured:
                    continue
                found.append({"path": relative, "type": documents.media_type(path.suffix)})
        return sorted(found, key=lambda item: item["path"].lower())

    def _sections(self, path: Path, raw: bytes, kind: str | None) -> tuple[Section, ...]:
        if kind == "pdf":
            return self._pdf_sections(path)
        return documents.text_sections(documents.decode(raw, str(path)), kind)

    def _supported_files(self, target: Path) -> list[Path]:
        return [
            path
            for path in target.rglob("*")
            if path.is_file() and documents.media_type(path.suffix) is not None
        ]

    def _reject_escaped_files(self, files: list[Path]) -> None:
        """A catalogued directory may still contain a symlink pointing out of the project."""
        root = self.project_root.resolve()
        for path in files:
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise KnowledgeError(
                    f"knowledge source contains an out-of-project link: {path}"
                ) from exc

    def _candidate_paths(self, root: Path) -> list[Path]:
        if root.is_file():
            paths = [root]
        elif root.is_dir():
            paths = list(root.rglob("*"))
        else:
            return []
        return [
            path
            for path in paths
            if path.is_file() and documents.media_type(path.suffix) is not None
        ]

    @staticmethod
    def _pdf_sections(path: Path) -> tuple[Section, ...]:
        """One section per page, so a citation can name the page it came from."""
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - installation contract
            raise KnowledgeError("PDF indexing requires the pypdf package") from exc
        try:
            reader = PdfReader(path)
            sections = []
            for number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    sections.append(Section("", number, text))
            return tuple(sections)
        except Exception as exc:
            raise KnowledgeError(f"could not extract PDF {path}: {exc}") from exc
