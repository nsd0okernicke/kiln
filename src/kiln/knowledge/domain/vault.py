"""
The vault config and the note-level rules an Obsidian vault adds.

Pure by design: config parsing takes a decoded payload, frontmatter and wikilink parsing
take decoded text, and exclusion matching takes path strings -- so every rejection and every
extraction the acceptance criteria name is testable without writing a file. Reading
`knowledge.json` belongs to `infrastructure.factory`, the disk to `infrastructure.vault_documents`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import KnowledgeError

VERSION = 2

#: The note suffixes a vault contributes. An Obsidian vault is markdown notes; PDFs, images
#: and media live in attachment folders and are excluded rather than parsed.
NOTE_SUFFIXES = (".md", ".markdown")

#: `---` fence at the very start of a note, its body captured, closing fence consumed.
#: CRLF-tolerant because a Windows-written note ends its lines with `\r\n`, and a fence
#: that only matched LF would silently turn every such note's frontmatter into prose.
FRONTMATTER_FENCE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)

#: `[[Note]]`, `[[folder/Note]]`, `[[Note#Heading]]`, `[[Note|alias]]` -- the target is what
#: resolves; the heading and the alias are presentation.
WIKILINK = re.compile(r"\[\[([^\[\]#|]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")


@dataclass(frozen=True)
class VaultConfig:
    """The parsed `kiln/project/knowledge.json` document."""

    #: The vault root as written in the config. Empty means deliberately not configured --
    #: a project without a vault must not read as a broken one.
    path: str = ""
    #: Vault-relative folder prefixes whose notes are not project knowledge.
    exclude: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.path.strip())


def empty_config() -> dict:
    """What `kiln init` scaffolds: the v2 shape with nothing configured yet."""
    return {"version": VERSION, "vault": "", "exclude": []}


def parse_config(payload: object) -> VaultConfig:
    """
    A decoded knowledge config, or `KnowledgeError` naming the fault.

    A v1 catalog file is a real state -- projects from before this change carry one -- so
    its error names the migration instead of speaking of a wrong version number.
    """
    if not isinstance(payload, dict):
        raise KnowledgeError("knowledge config must be a JSON object")
    _require_version(payload.get("version"))
    vault_path = payload.get("vault", "")
    if not isinstance(vault_path, str):
        raise KnowledgeError("knowledge config vault must be a string path")
    return VaultConfig(vault_path.strip(), _exclude_entries(payload))


def _require_version(version: object) -> None:
    if version == 1:
        raise KnowledgeError(
            "knowledge.json is a v1 source catalog; replace it with a vault config: "
            '{"version": 2, "vault": "<path to obsidian vault>", "exclude": []}'
        )
    if version != VERSION:
        raise KnowledgeError(f"knowledge config must have version {VERSION}")


def _exclude_entries(payload: dict) -> tuple[str, ...]:
    exclude = payload.get("exclude", [])
    if not isinstance(exclude, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in exclude
    ):
        raise KnowledgeError("knowledge config exclude must be a list of non-empty strings")
    return tuple(entry.strip() for entry in exclude)


def parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """
    A note's YAML frontmatter and its body.

    Only the two shapes Obsidian's own properties editor writes are read -- `key: value`
    scalars and `key:` block lists -- and everything else is left where it is: a malformed
    fence is prose, not an error, and a hand-written note must stay indexable. Keys are
    lowercased because Obsidian writes properties in lowercase.
    """
    match = FRONTMATTER_FENCE.match(content)
    if not match:
        return {}, content
    meta: dict[str, object] = {}
    current: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            current = _meta_entry(line, meta, current)
    return meta, content[match.end() :]


def _meta_entry(line: str, meta: dict[str, object], current: str | None) -> str | None:
    """One frontmatter line folded into the metadata. Returns the key block items extend."""
    if line.startswith("- "):
        _extend_block(_unquote(line[2:].strip()), meta, current)
        return current
    key, separator, value = line.partition(":")
    if not separator:
        return None
    key = key.strip().lower()
    value = value.strip()
    meta[key] = _inline_list(value) if value.startswith("[") else _unquote(value)
    return key


def _extend_block(value: str, meta: dict[str, object], current: str | None) -> None:
    if current is None or current not in meta:
        return
    existing = meta[current]
    if isinstance(existing, list):
        meta[current] = [*existing, value]
    elif existing:
        meta[current] = [existing, value]
    else:
        # A `key:` line with no value is a list being written, not an empty scalar:
        # keeping the empty string would make the first block item arrive as "".
        meta[current] = [value]


def frontmatter_title(meta: dict[str, object]) -> str | None:
    value = meta.get("title")
    return value.strip() if isinstance(value, str) and value.strip() else None


def frontmatter_status(meta: dict[str, object]) -> str | None:
    value = meta.get("status")
    return value.strip() if isinstance(value, str) and value.strip() else None


def frontmatter_tags(meta: dict[str, object]) -> tuple[str, ...]:
    """
    A note's tags, from a block list, an inline list or a comma-separated scalar.

    All three are things a hand-written note or the properties editor produces, and a tag
    that arrived as the wrong one of the three is still a tag.
    """
    value = meta.get("tags")
    if isinstance(value, list):
        return _string_tags(value)
    if isinstance(value, str) and value.strip():
        return _comma_tags(value)
    return ()


def _string_tags(items: list) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in items if str(item).strip())


def _comma_tags(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def metadata_block(meta: dict[str, object], title: str) -> str:
    """
    The note's facts as indexed text, prepended to the body before sectioning.

    The title, tags and status ride along in the indexed content so a search for any of
    them finds the note -- the note stem is the primary lookup key in a vault, and a tag
    search that cannot find its note would be a search that answers nothing.
    """
    lines = [f"title: {title}"]
    tags = frontmatter_tags(meta)
    if tags:
        lines.append("tags: " + ", ".join(tags))
    status = frontmatter_status(meta)
    if status:
        lines.append("status: " + status)
    return "\n".join(lines)


def wikilinks(content: str) -> tuple[str, ...]:
    """The notes a note links to, deduplicated in first-appearance order."""
    seen: dict[str, None] = {}
    for match in WIKILINK.finditer(content):
        target = match.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return tuple(seen)


def is_excluded(relative_path: str, exclude: tuple[str, ...]) -> bool:
    """
    Whether one vault-relative note path falls under an excluded prefix.

    POSIX separators and case-insensitive on purpose: the config is written with the folder
    names as the vault shows them ("01 Inbox"), and a Windows filesystem compares paths
    case-insensitively, so `01 inbox/note.md` and `01 Inbox/note.md` are the same note.
    """
    normalized = relative_path.replace("\\", "/").lower()
    for entry in exclude:
        prefix = entry.replace("\\", "/").strip("/").lower()
        if not prefix:
            continue
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _inline_list(value: str) -> list[str]:
    inner = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    return [_unquote(item.strip()) for item in inner.split(",") if item.strip()]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
