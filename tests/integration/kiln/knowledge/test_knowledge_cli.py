from __future__ import annotations

import json

import pytest

from kiln.knowledge.infrastructure import cli
from kiln.launcher.domain.paths import KilnPaths
from kiln.launcher.infrastructure.cli import _sync_knowledge

pytestmark = pytest.mark.integration


def run(root, *arguments: str) -> int:
    return cli.main(["--working-dir", str(root), *arguments])


def write_config(root, vault_path: str, *exclude: str) -> None:
    path = root / "kiln" / "project" / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 2, "vault": vault_path, "exclude": list(exclude)}),
        encoding="utf-8",
    )


def make_vault(root, name="vault") -> object:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_public_cli_syncs_searches_and_shows_a_vault(tmp_path, capsys):
    write_config(tmp_path, str(tmp_path / "vault"))
    make_vault(tmp_path)
    (tmp_path / "vault" / "policy.md").write_text(
        "# Returns\nCustomers may return an unread book within thirty days.", encoding="utf-8"
    )

    assert run(tmp_path, "status", "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configured"] is True
    assert status["last_run"] is None

    assert run(tmp_path, "sync", "--json") == 0
    sync = json.loads(capsys.readouterr().out)
    assert sync["updated"] == 1

    assert run(tmp_path, "search", "return", "--json") == 0
    results = json.loads(capsys.readouterr().out)
    assert results[0]["heading"] == "Returns"
    assert results[0]["freshness"] == "indexed"
    assert results[0]["source_id"] == "vault"

    assert run(tmp_path, "show", results[0]["document_id"], "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert "thirty days" in document["content"]

    # The same index rebuilt incrementally: nothing changed, so everything is skipped.
    assert run(tmp_path, "sync", "--json") == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 1


def test_show_names_related_notes_in_both_output_modes(tmp_path, capsys):
    write_config(tmp_path, str(tmp_path / "vault"))
    make_vault(tmp_path)
    (tmp_path / "vault" / "a.md").write_text("See [[b]].", encoding="utf-8")
    (tmp_path / "vault" / "b.md").write_text("Back to [[a]].", encoding="utf-8")
    run(tmp_path, "sync")
    capsys.readouterr()

    run(tmp_path, "search", "back", "--json")
    document_id = json.loads(capsys.readouterr().out)[0]["document_id"]

    assert run(tmp_path, "show", document_id, "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert document["related"] == ["a.md"]

    assert run(tmp_path, "show", document_id) == 0
    assert "Related: a.md" in capsys.readouterr().out


def test_status_on_an_unconfigured_project_names_the_config_file(tmp_path, capsys):
    (tmp_path / "kiln" / "project").mkdir(parents=True)
    (tmp_path / "kiln" / "project" / "knowledge.json").write_text(
        '{"version": 2, "vault": "", "exclude": []}\n', encoding="utf-8"
    )

    assert run(tmp_path, "status") == 0
    assert "not configured" in capsys.readouterr().out

    assert run(tmp_path, "sync", "--json") == 1
    assert "not configured" in capsys.readouterr().out


def test_a_v1_catalog_names_the_migration_instead_of_syncing(tmp_path, capsys):
    (tmp_path / "kiln" / "project").mkdir(parents=True)
    (tmp_path / "kiln" / "project" / "knowledge.json").write_text(
        '{"version": 1, "sources": []}\n', encoding="utf-8"
    )

    assert run(tmp_path, "status") == 1
    assert "v1 source catalog" in capsys.readouterr().out

    assert run(tmp_path, "sync") == 1
    assert "v1 source catalog" in capsys.readouterr().out

    assert run(tmp_path, "search", "anything") == 1
    assert "v1 source catalog" in capsys.readouterr().out


def test_excluded_folders_are_not_indexed(tmp_path, capsys):
    write_config(tmp_path, str(tmp_path / "vault"), "01 Inbox")
    make_vault(tmp_path)
    (tmp_path / "vault" / "knowledge.md").write_text("# KB\nindexed", encoding="utf-8")
    inbox = tmp_path / "vault" / "01 Inbox"
    inbox.mkdir(parents=True)
    (inbox / "note.md").write_text("churn", encoding="utf-8")

    assert run(tmp_path, "sync", "--json") == 0
    assert json.loads(capsys.readouterr().out)["updated"] == 1

    assert run(tmp_path, "search", "churn", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_launch_refreshes_the_configured_vault_and_skips_an_unconfigured_one(tmp_path, caplog):
    write_config(tmp_path, str(tmp_path / "vault"))
    make_vault(tmp_path)
    (tmp_path / "vault" / "a.md").write_text("alpha", encoding="utf-8")

    _sync_knowledge(KilnPaths.create(tmp_path, tmp_path))

    assert (tmp_path / ".kiln" / "knowledge.db").is_file()

    # An unconfigured project must launch without noise about knowledge.
    caplog.clear()
    write_config(tmp_path, "")
    (tmp_path / ".kiln" / "knowledge.db").unlink()
    _sync_knowledge(KilnPaths.create(tmp_path, tmp_path))

    assert not (tmp_path / ".kiln" / "knowledge.db").is_file()
    assert "knowledge" not in caplog.text


def test_launch_without_a_knowledge_config_syncs_nothing(tmp_path):
    _sync_knowledge(KilnPaths.create(tmp_path, tmp_path))

    assert not (tmp_path / ".kiln" / "knowledge.db").is_file()


def test_launch_with_a_missing_vault_defers_and_warns_about_nothing(tmp_path, caplog):
    write_config(tmp_path, str(tmp_path / "no-such-vault"))

    _sync_knowledge(KilnPaths.create(tmp_path, tmp_path))

    assert "knowledge source could not be indexed" not in caplog.text
    assert "knowledge index could not be refreshed" not in caplog.text


def test_plain_text_output_names_where_the_answer_came_from(tmp_path, capsys):
    """
    The default output, not `--json`. A result an agent cannot attribute is not citable, so
    the document id, source title, path and heading all have to be on screen.
    """
    write_config(tmp_path, str(tmp_path / "vault"))
    make_vault(tmp_path)
    (tmp_path / "vault" / "policy.md").write_text(
        "# Cancellation\nSubscriptions have a fourteen day cooling-off period.", encoding="utf-8"
    )
    assert run(tmp_path, "sync") == 0
    capsys.readouterr()

    assert run(tmp_path, "search", "cooling-off") == 0

    output = capsys.readouterr().out
    assert "vault" in output
    assert "policy.md" in output, "the citation names the vault-relative note path"
    assert "Cancellation" in output
    assert "fourteen day cooling-off period" in output


def test_plain_text_status_shows_the_vault_and_the_last_sync(tmp_path, capsys):
    write_config(tmp_path, str(tmp_path / "vault"), "01 Inbox")
    make_vault(tmp_path)
    (tmp_path / "vault" / "a.md").write_text("alpha", encoding="utf-8")
    run(tmp_path, "sync")
    capsys.readouterr()

    assert run(tmp_path, "status") == 0

    output = capsys.readouterr().out
    assert "vault" in output
    assert "excluded: 01 Inbox" in output
    assert "last sync:" in output
    assert "notes indexed: 1" in output


def test_search_rejects_an_out_of_range_result_limit(tmp_path, capsys):
    write_config(tmp_path, str(tmp_path / "vault"))
    assert run(tmp_path, "search", "anything", "--max-results", "0") == 1
    assert "--max-results must be between 1 and 100" in capsys.readouterr().out


def test_sync_reports_an_unreadable_config(tmp_path, capsys):
    (tmp_path / "kiln" / "project").mkdir(parents=True)
    (tmp_path / "kiln" / "project" / "knowledge.json").write_text("{broken", encoding="utf-8")

    assert run(tmp_path, "sync") == 1
    assert "could not read knowledge config" in capsys.readouterr().out
