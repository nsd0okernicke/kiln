"""
An Obsidian vault behind the knowledge use cases.

Everything that needs the disk. The text rules -- frontmatter parsing, wikilink extraction,
exclusion matching -- live in `domain.vault` and are called from here rather than
reimplemented, which is what lets those be tested as string transformations.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..domain import documents, vault
from ..domain.models import ExtractedDocument, KnowledgeError, Source
from ..domain.vault import VaultConfig

#: The single indexed origin. One vault is one source; per-note rows live in `documents`.
SOURCE_ID = "vault"


class VaultDocumentSource:
    """`DocumentSource` over one Obsidian vault."""

    def __init__(self, config: VaultConfig, project_root: Path):
        # A relative vault path resolves against the project, so a vault kept inside the
        # project tree works; an absolute one is the external-vault case this exists for.
        self.root = _resolve(config.path, project_root)
        self.exclude = config.exclude

    def source(self) -> Source:
        return Source(SOURCE_ID, self.root.as_posix(), self.root.name or SOURCE_ID, "vault")

    def supports(self) -> bool:
        """A vault that is not mounted right now is deferred, not failed -- documents stay."""
        return self.root.is_dir()

    def discover(self) -> list[str]:
        if not self.root.is_dir():
            raise KnowledgeError(f"vault not found: {self.root}")
        notes = [self._key(path) for path in self.root.rglob("*") if self._is_note(path)]
        return sorted(notes, key=str.lower)

    def _is_note(self, path: Path) -> bool:
        return (
            path.is_file()
            and path.suffix.lower() in vault.NOTE_SUFFIXES
            and not self._skipped(path)
        )

    def fingerprint(self, key: str) -> str:
        return documents.fingerprint(self._note(key).read_bytes())

    def extract(self, key: str) -> ExtractedDocument:
        path = self._note(key)
        raw = path.read_bytes()
        meta, body = vault.parse_frontmatter(documents.decode(raw, key))
        title = vault.frontmatter_title(meta) or path.stem
        metadata = vault.metadata_block(meta, title)
        text = f"{metadata}\n\n{body.strip()}" if metadata else body
        return documents.build_document(
            relative_path=key,
            title=title,
            sections=documents.note_sections(text),
            raw=raw,
        )

    def related(self, key: str) -> list[str]:
        """
        The existing notes this note links to, resolved.

        Wikilinks name note stems (or vault-relative paths) without the suffix, so a link is
        matched against the discovered note set case-insensitively; a link to a note that
        does not exist is dropped rather than returned -- a red link is not knowledge.
        """
        try:
            content = documents.decode(self._note(key).read_bytes(), key)
        except (KnowledgeError, OSError):
            return []
        _, body = vault.parse_frontmatter(content)
        notes = self._link_targets()
        resolved = [
            notes[target.lower()] for target in vault.wikilinks(body) if target.lower() in notes
        ]
        return [note for note in dict.fromkeys(resolved) if note != key]

    def _link_targets(self) -> dict[str, str]:
        """Every discovered note by its linkable names: stem, path, and path without suffix."""
        targets: dict[str, str] = {}
        for note in self.discover():
            posix = PurePosixPath(note)
            targets.setdefault(posix.stem.lower(), note)
            targets.setdefault(note.lower(), note)
            targets.setdefault(posix.with_suffix("").as_posix().lower(), note)
        return targets

    # -- internals --------------------------------------------------------------------------

    def _skipped(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        # Dot-names cover `.obsidian`, `.git`, `.trash` and agent session state in one rule.
        if any(part.startswith(".") for part in relative.parts):
            return True
        return vault.is_excluded(self._key(path), self.exclude)

    def _key(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _note(self, key: str) -> Path:
        """A discovered key back to the file it names. Re-checked, never trusted blindly."""
        path = (self.root / key).resolve(strict=False)
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise KnowledgeError(f"knowledge note escapes the vault: {key}") from exc
        return path


def _resolve(path: str, project_root: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else project_root / candidate
