from __future__ import annotations

import json

import pytest

from kiln.knowledge.infrastructure import cli
from kiln.launcher.domain.paths import KilnPaths
from kiln.launcher.infrastructure.cli import _sync_knowledge

pytestmark = pytest.mark.integration


def run(root, *arguments: str) -> int:
    return cli.main(["--working-dir", str(root), *arguments])


def initialize(root) -> None:
    catalog = root / "kiln" / "project" / "knowledge.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"version": 1, "sources": []}\n', encoding="utf-8")


def test_public_cli_manages_syncs_searches_and_shows_sources(tmp_path, capsys):
    initialize(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "policy.md").write_text(
        "# Returns\nCustomers may return an unread book within thirty days.", encoding="utf-8"
    )

    assert (
        run(
            tmp_path,
            "add",
            "docs/policy.md",
            "--id",
            "returns",
            "--title",
            "Returns policy",
            "--tag",
            "product",
        )
        == 0
    )
    assert run(tmp_path, "sources", "--json") == 0
    sources = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert sources[0]["tags"] == ["product"]

    assert run(tmp_path, "sync", "--json") == 0
    sync = json.loads(capsys.readouterr().out)
    assert sync["updated"] == 1

    assert run(tmp_path, "search", "return", "--json") == 0
    results = json.loads(capsys.readouterr().out)
    assert results[0]["heading"] == "Returns"
    assert results[0]["freshness"] == "indexed"

    assert run(tmp_path, "show", results[0]["document_id"], "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert "thirty days" in document["content"]

    assert run(tmp_path, "remove", "returns", "--json") == 0
    capsys.readouterr()
    assert run(tmp_path, "sync", "--json") == 0
    assert json.loads(capsys.readouterr().out)["removed"] >= 1


def test_setup_discovers_candidates_without_mutating_the_catalog(tmp_path, capsys):
    initialize(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "adr.md").write_text("# Decision", encoding="utf-8")

    assert run(tmp_path, "setup", "--json") == 0

    assert json.loads(capsys.readouterr().out) == [{"path": "docs/adr.md", "type": "markdown"}]
    assert run(tmp_path, "sources", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_explicit_sync_reports_source_failures(tmp_path, capsys):
    initialize(tmp_path)
    assert run(tmp_path, "add", "missing.txt", "--id", "missing", "--type", "text") == 0
    capsys.readouterr()

    assert run(tmp_path, "sync", "--json") == 1

    result = json.loads(capsys.readouterr().out)
    assert result["failed"] == 1
    assert "source not found" in result["failures"][0]


def test_cli_rejects_invalid_identity_and_out_of_project_path(tmp_path, capsys):
    initialize(tmp_path)

    assert run(tmp_path, "add", "doc.md", "--id", "Bad ID", "--type", "markdown") == 1
    assert "invalid knowledge source id" in capsys.readouterr().out

    assert run(tmp_path, "add", "../outside.md", "--id", "outside", "--type", "markdown") == 1
    assert "escapes the project" in capsys.readouterr().out


def test_launch_refreshes_incrementally_and_only_warns_about_failed_sources(tmp_path, caplog):
    initialize(tmp_path)
    assert run(tmp_path, "add", "missing.txt", "--id", "missing", "--type", "text") == 0
    caplog.clear()

    _sync_knowledge(KilnPaths.create(tmp_path, tmp_path))

    assert (tmp_path / ".kiln" / "knowledge.db").is_file()
    assert "knowledge source could not be indexed" in caplog.text


def test_cli_adds_a_url_source_without_being_told_its_type(tmp_path, capsys):
    """The scheme is enough; requiring `--type url` beside an obvious URL is a papercut."""
    initialize(tmp_path)

    assert run(tmp_path, "add", "https://docs.example.com/api/rate-limits", "--json") == 0

    entry = json.loads(capsys.readouterr().out)
    assert entry["type"] == "url"
    assert entry["url"] == "https://docs.example.com/api/rate-limits"
    assert "path" not in entry, "a url source carries no path"

    assert run(tmp_path, "sources") == 0
    assert "https://docs.example.com/api/rate-limits" in capsys.readouterr().out


def test_cli_refuses_a_url_that_would_leak_credentials_into_the_catalog(tmp_path, capsys):
    initialize(tmp_path)

    assert run(tmp_path, "add", "https://user:secret@example.com/doc", "--id", "doc") == 1

    output = capsys.readouterr().out
    assert "credentials" in output
    assert "secret" not in json.dumps(
        json.loads((tmp_path / "kiln" / "project" / "knowledge.json").read_text())
    )


def test_offline_sync_leaves_url_sources_alone_and_still_succeeds(tmp_path, capsys):
    initialize(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "local.md").write_text("# Local\nindexed anyway", encoding="utf-8")
    assert run(tmp_path, "add", "docs/local.md", "--id", "local") == 0
    assert run(tmp_path, "add", "https://example.invalid/doc", "--id", "remote") == 0
    capsys.readouterr()

    # No network is touched: --offline defers the url rather than attempting a fetch.
    assert run(tmp_path, "sync", "--offline", "--json") == 0

    result = json.loads(capsys.readouterr().out)
    assert result["deferred"] == ["remote"]
    assert result["failed"] == 0
    assert result["updated"] == 1

    # The human-readable output has to name them too; only --json did, at first.
    assert run(tmp_path, "sync", "--offline") == 0
    assert "not refreshed: remote" in capsys.readouterr().out


def test_plain_text_search_output_names_where_the_answer_came_from(tmp_path, capsys):
    """
    The default output, not `--json`. A result an agent cannot attribute is not citable, so
    the document id, source title, path and heading all have to be on screen.
    """
    initialize(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "policy.md").write_text(
        "# Cancellation\nSubscriptions have a fourteen day cooling-off period.", encoding="utf-8"
    )
    assert run(tmp_path, "add", "docs/policy.md", "--id", "policy", "--title", "Policy") == 0
    assert run(tmp_path, "sync") == 0
    capsys.readouterr()

    assert run(tmp_path, "search", "cooling-off") == 0

    output = capsys.readouterr().out
    assert "Policy" in output
    assert "docs/policy.md" in output
    assert "Cancellation" in output
    assert "fourteen day cooling-off period" in output


def test_search_rejects_an_out_of_range_result_limit(tmp_path, capsys):
    initialize(tmp_path)
    assert run(tmp_path, "search", "anything", "--max-results", "0") == 1
    assert "--max-results must be between 1 and 100" in capsys.readouterr().out
