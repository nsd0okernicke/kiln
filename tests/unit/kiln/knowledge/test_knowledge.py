from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln.knowledge.domain import documents, vault
from kiln.knowledge.domain.models import KnowledgeError
from kiln.knowledge.infrastructure.factory import build_service, index_path
from kiln.knowledge.infrastructure.vault_documents import VaultDocumentSource


def write_config(root: Path, vault_path: str, *exclude: str) -> None:
    path = root / "kiln" / "project" / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 2, "vault": vault_path, "exclude": list(exclude)}),
        encoding="utf-8",
    )


def make_vault(root: Path, name: str = "vault") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def note(vault_root: Path, relative: str, content: str) -> None:
    path = vault_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestConfig:
    def test_parses_a_v2_config(self):
        config = vault.parse_config(
            {"version": 2, "vault": "C:/vaults/kb", "exclude": ["01 Inbox"]}
        )

        assert config.path == "C:/vaults/kb"
        assert config.exclude == ("01 Inbox",)
        assert config.configured

    def test_an_empty_vault_is_not_configured(self):
        assert not vault.parse_config({"version": 2, "vault": "", "exclude": []}).configured
        assert not vault.parse_config({"version": 2}).configured

    def test_exclude_is_optional(self):
        assert vault.parse_config({"version": 2, "vault": "kb"}).exclude == ()

    def test_a_v1_catalog_names_the_migration(self):
        with pytest.raises(KnowledgeError, match="v1 source catalog"):
            vault.parse_config({"version": 1, "sources": []})

    @pytest.mark.parametrize(
        "payload,error",
        [
            ("not an object", "must be a JSON object"),
            ({"version": 3, "vault": "kb"}, "version 2"),
            ({"version": 2, "vault": 42}, "must be a string path"),
            ({"version": 2, "vault": "kb", "exclude": "01 Inbox"}, "list of non-empty strings"),
            ({"version": 2, "vault": "kb", "exclude": [""]}, "list of non-empty strings"),
        ],
    )
    def test_rejects_invalid_configs(self, payload, error):
        with pytest.raises(KnowledgeError, match=error):
            vault.parse_config(payload)

    def test_the_scaffolded_config_is_the_empty_v2_shape(self):
        assert vault.parse_config(vault.empty_config()) == vault.VaultConfig()


class TestFrontmatter:
    def test_block_list_tags_and_scalar_status(self):
        meta, body = vault.parse_frontmatter(
            "---\ntags:\n  - plm\n  - cdb\nstatus: done\n---\n\n# Body\nprose"
        )

        assert vault.frontmatter_tags(meta) == ("plm", "cdb")
        assert vault.frontmatter_status(meta) == "done"
        assert body.startswith("\n# Body")

    def test_inline_list_tags_and_a_comma_separated_scalar(self):
        inline, _ = vault.parse_frontmatter("---\ntags: [plm, cdb]\n---\nbody")
        comma, _ = vault.parse_frontmatter("---\ntags: plm, cdb\n---\nbody")

        assert vault.frontmatter_tags(inline) == ("plm", "cdb")
        assert vault.frontmatter_tags(comma) == ("plm", "cdb")

    def test_quoted_values_are_unquoted(self):
        meta, _ = vault.parse_frontmatter(
            '---\nstatus: "in progress"\ntitle: ' + "'A: Note'" + "\n---\nbody"
        )

        assert vault.frontmatter_status(meta) == "in progress"
        assert vault.frontmatter_title(meta) == "A: Note"

    def test_keys_are_lowercased(self):
        meta, _ = vault.parse_frontmatter("---\nTags:\n  - plm\n---\nbody")

        assert vault.frontmatter_tags(meta) == ("plm",)

    def test_a_malformed_fence_is_prose_not_an_error(self):
        """A note that starts with an unclosed fence must stay indexable as a body."""
        meta, body = vault.parse_frontmatter("---\ntags: plm\nno closing fence")

        assert meta == {}
        assert "tags" in body

    def test_no_frontmatter_returns_the_content(self):
        meta, body = vault.parse_frontmatter("# Just a note")

        assert meta == {}
        assert body == "# Just a note"

    def test_block_items_under_a_scalar_key_become_a_list(self):
        meta, _ = vault.parse_frontmatter("---\ntags: plm\n  - cdb\n---\nbody")

        assert vault.frontmatter_tags(meta) == ("plm", "cdb")


class TestWikilinks:
    def test_alias_and_heading_are_dropped_and_links_deduplicated(self):
        content = "See [[xBOMs]] then [[xBOMs#Structure|the structure]] and [[PowerScript]]."
        links = vault.wikilinks(content)

        assert links == ("xBOMs", "PowerScript")

    def test_a_path_link_keeps_its_path(self):
        assert vault.wikilinks("[[CONTACT Elements/REST API]]") == ("CONTACT Elements/REST API",)

    def test_no_links_is_empty(self):
        assert vault.wikilinks("plain text and [markdown](links.md)") == ()


class TestExclusion:
    @pytest.mark.parametrize(
        "path,exclude,excluded",
        [
            ("01 Inbox/2026-10-05.md", ("01 Inbox",), True),
            ("01 Inbox/2026-10-05.md", ("01 inbox",), True),
            ("01 Inbox", ("01 Inbox",), True),
            ("01 Inbox/nested/deep.md", ("01 Inbox",), True),
            ("04 Resources/xBOMs.md", ("01 Inbox", "05 Daily Notes"), False),
            ("04 Resources/xBOMs.md", (), False),
            ("01 Inbox 2/note.md", ("01 Inbox",), False),
            ("windows\\path\\note.md", ("windows/path",), True),
        ],
    )
    def test_prefix_matching(self, path, exclude, excluded):
        assert vault.is_excluded(path, exclude) is excluded


class TestVaultExtraction:
    def test_hidden_and_excluded_folders_are_not_discovered(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(vault_root, "04 Resources/xBOMs.md", "# xBOMs")
        note(vault_root, "01 Inbox/note.md", "inbox")
        note(vault_root, ".obsidian/plugins/note.md", "config")
        note(vault_root, ".trash/old.md", "deleted")
        note(vault_root, "04 Resources/attachment.png", "png")

        discovered = VaultDocumentSource(
            vault.VaultConfig(str(vault_root), ("01 Inbox",)), tmp_path
        ).discover()

        assert discovered == ["04 Resources/xBOMs.md"]

    def test_frontmatter_metadata_is_indexed_and_body_preserved(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(
            vault_root,
            "xBOMs.md",
            "---\ntags:\n  - plm\n  - cdb\nstatus: done\n---\n# xBOMs\n"
            "An assembly BOM lists parts.",
        )
        source = VaultDocumentSource(vault.VaultConfig(str(vault_root)), tmp_path)

        extracted = source.extract("xBOMs.md")

        assert extracted.title == "xBOMs"
        assert extracted.media_type == "markdown"
        assert extracted.content.startswith("title: xBOMs\ntags: plm, cdb\nstatus: done")
        assert "---" not in extracted.content, "the frontmatter fence is not indexed"
        # Headings are provenance, not prose: they name the section, the body carries the text.
        assert extracted.sections[-1].heading == "xBOMs"
        assert "An assembly BOM lists parts." in extracted.content
        assert all(len(section.text) <= documents.MAX_CHUNK_CHARS for section in extracted.sections)

    def test_a_frontmatter_title_wins_over_the_stem(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(vault_root, "2026-10-05.md", "---\ntitle: Daily Log\n---\nbody")

        extracted = VaultDocumentSource(vault.VaultConfig(str(vault_root)), tmp_path).extract(
            "2026-10-05.md"
        )

        assert extracted.title == "Daily Log"

    def test_a_large_section_is_split(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(
            vault_root,
            "big.md",
            "# Policy\n" + "word " * 1000,
        )
        source = VaultDocumentSource(vault.VaultConfig(str(vault_root)), tmp_path)

        extracted = source.extract("big.md")

        assert len(extracted.sections) > 1

    def test_a_relative_vault_path_resolves_against_the_project(self, tmp_path):
        note(make_vault(tmp_path / "project" / "docs", "kb"), "a.md", "body")

        source = VaultDocumentSource(vault.VaultConfig("docs/kb"), tmp_path / "project")

        assert source.discover() == ["a.md"]

    def test_a_note_that_is_not_utf8_fails_that_note(self, tmp_path):
        vault_root = make_vault(tmp_path)
        (vault_root / "latin.md").write_bytes("ä".encode("latin-1"))
        source = VaultDocumentSource(vault.VaultConfig(str(vault_root)), tmp_path)

        with pytest.raises(KnowledgeError, match="not UTF-8"):
            source.extract("latin.md")

    def test_a_key_that_escapes_the_vault_is_refused(self, tmp_path):
        source = VaultDocumentSource(vault.VaultConfig(str(make_vault(tmp_path))), tmp_path)

        with pytest.raises(KnowledgeError, match="escapes the vault"):
            source.fingerprint("../outside.md")


class TestRelatedNotes:
    def _source(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(vault_root, "xBOMs.md", "# xBOMs\nSee [[PowerScript]] and [[REST API]].")
        note(vault_root, "PowerScript.md", "---\ntags: code\n---\nBack to [[xBOMs]].")
        note(vault_root, "nested/REST API.md", "link to [[xBOMs#Structure|structure]]")
        note(vault_root, "orphan.md", "links to [[Missing Note]] and [[orphan]]")
        return vault_root

    def test_links_resolve_by_stem_and_path_case_insensitively(self, tmp_path):
        self._source(tmp_path)
        source = VaultDocumentSource(vault.VaultConfig(str(tmp_path / "vault")), tmp_path)

        assert source.related("xBOMs.md") == ["PowerScript.md", "nested/REST API.md"]
        assert source.related("nested/REST API.md") == ["xBOMs.md"]

    def test_red_links_and_self_links_are_dropped(self, tmp_path):
        self._source(tmp_path)
        source = VaultDocumentSource(vault.VaultConfig(str(tmp_path / "vault")), tmp_path)

        assert source.related("orphan.md") == []

    def test_a_missing_note_yields_nothing(self, tmp_path):
        self._source(tmp_path)
        source = VaultDocumentSource(vault.VaultConfig(str(tmp_path / "vault")), tmp_path)

        assert source.related("ghost.md") == []


class TestKnowledgeService:
    def _service(self, tmp_path, vault_path="vault", *exclude):
        write_config(tmp_path, vault_path, *exclude)
        return build_service(tmp_path)

    def test_syncs_searches_and_shows_notes(self, tmp_path):
        note(
            make_vault(tmp_path),
            "04 Resources/xBOMs.md",
            "# Cancellation\nFourteen days cooling-off.",
        )
        service = self._service(tmp_path)

        sync = service.sync()
        results = service.search("cooling-off")
        shown = service.show(results[0].document_id)

        assert sync.updated == 1 and not sync.failures and not sync.deferred
        assert results[0].heading == "Cancellation"
        assert results[0].path == "04 Resources/xBOMs.md"
        assert results[0].source_id == "vault"
        assert "cooling-off" in shown["content"]

    def test_metadata_is_searchable(self, tmp_path):
        note(
            make_vault(tmp_path),
            "note.md",
            "---\ntags:\n  - plm\nstatus: done\n---\n# Body\nprose",
        )
        service = self._service(tmp_path)
        service.sync()

        results = service.search("plm")

        assert results and results[0].path == "note.md"

    def test_incremental_sync_updates_and_removes_notes(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(vault_root, "first.md", "alpha")
        note(vault_root, "second.md", "beta")
        service = self._service(tmp_path)

        assert service.sync().updated == 2
        (vault_root / "first.md").write_text("alpha changed", encoding="utf-8")
        (vault_root / "second.md").unlink()
        result = service.sync()

        assert result.updated == 1
        assert result.removed == 1
        assert service.search("changed")
        assert service.search("beta") == []

    def test_an_unchanged_vault_is_skipped_wholesale(self, tmp_path):
        note(make_vault(tmp_path), "a.md", "alpha")
        service = self._service(tmp_path)
        service.sync()

        result = service.sync()

        assert result.skipped == 1 and result.updated == 0

    def test_a_failing_note_is_not_served_stale(self, tmp_path):
        vault_root = make_vault(tmp_path)
        (vault_root / "latin.md").write_bytes("ä".encode("latin-1"))
        service = self._service(tmp_path)

        result = service.sync()

        assert result.failures
        assert service.search("ä") == []

    def test_a_missing_vault_is_deferred_and_stays_searchable(self, tmp_path):
        note(make_vault(tmp_path), "a.md", "enduring knowledge")
        service = self._service(tmp_path)
        service.sync()
        service.index.close()

        service = self._service(tmp_path, vault_path="elsewhere")
        result = service.sync()

        assert result.deferred == ["vault"] and not result.failures
        assert service.search("enduring knowledge"), "the unmounted vault keeps its documents"

    def test_an_unconfigured_vault_refuses_to_sync(self, tmp_path):
        write_config(tmp_path, "")
        service = build_service(tmp_path)

        with pytest.raises(KnowledgeError, match="not configured"):
            service.sync()

    def test_a_deleted_database_is_rebuilt(self, tmp_path):
        note(make_vault(tmp_path), "a.md", "reconstructible knowledge")
        service = self._service(tmp_path)
        service.sync()
        index_path(tmp_path).unlink()

        service.sync()

        assert service.search("reconstructible")

    def test_legacy_source_rows_are_cleared_by_a_vault_sync(self, tmp_path):
        note(make_vault(tmp_path), "a.md", "alpha")
        service = self._service(tmp_path)
        service.sync()
        # Simulate a pre-vault sync: a second source row the vault cannot explain.
        legacy = type(service.index)(index_path(tmp_path))  # a fresh index over the same database
        legacy.connection.execute(
            "INSERT INTO sources(id,title,path,type,tags,status)"
            " VALUES('old','Old','x','text','','indexed')"
        )
        legacy.connection.commit()
        legacy.close()

        result = service.sync()

        assert result.updated == 0, "the vault notes are unchanged"
        assert service.status()["documents"] == 1, "only the vault's notes remain"

    def test_show_carries_related_notes(self, tmp_path):
        vault_root = make_vault(tmp_path)
        note(vault_root, "a.md", "See [[b]].")
        note(vault_root, "b.md", "Back to [[a]].")
        service = self._service(tmp_path)
        service.sync()

        shown = service.show(service.search("b")[0].document_id)

        # Searching "b" finds the note that mentions it (a.md); its link resolves to b.md.
        assert shown["related"] == ["b.md"]

    def test_status_reports_configuration_and_the_last_sync(self, tmp_path):
        note(make_vault(tmp_path), "a.md", "alpha")
        service = self._service(tmp_path, "vault", "01 Inbox")
        service.sync()

        status = service.status()

        assert status["configured"] is True
        assert status["vault"].endswith("vault")
        assert status["exclude"] == ["01 Inbox"]
        assert status["documents"] == 1
        assert status["last_run"]["updated"] == 1
        assert status["source"]["status"] == "indexed"

    def test_status_without_a_config_reports_not_configured(self, tmp_path):
        service = build_service(tmp_path)

        status = service.status()

        assert status["configured"] is False
        assert status["last_run"] is None
        assert status["documents"] == 0

    def test_search_without_an_index_names_the_sync(self, tmp_path):
        service = build_service(tmp_path)

        with pytest.raises(KnowledgeError, match="kiln knowledge sync"):
            service.search("anything")
