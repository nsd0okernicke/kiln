"""Acceptance scenario: public commands build and query vault-based project knowledge."""

import json

from conftest import REPO_ROOT, console_script


def test_an_initialized_project_scaffolds_an_unconfigured_vault(tmp_path, command_runner):
    project = tmp_path / "knowledge-project"
    initialized = command_runner.run(console_script("kiln"), "init", project, cwd=REPO_ROOT)
    assert initialized.returncode == 0

    config = json.loads((project / "kiln" / "project" / "knowledge.json").read_text())
    assert config["version"] == 2
    assert config["vault"] == ""

    status = command_runner.run(
        console_script("kiln"), "knowledge", "--working-dir", project, "status", cwd=REPO_ROOT
    )
    assert status.returncode == 0
    assert "not configured" in status.stdout


def test_syncs_and_retrieves_vault_knowledge(tmp_path, command_runner):
    project = tmp_path / "knowledge-project"
    initialized = command_runner.run(console_script("kiln"), "init", project, cwd=REPO_ROOT)
    assert initialized.returncode == 0

    vault = tmp_path / "knowledge-vault"
    (vault / "04 Resources").mkdir(parents=True)
    (vault / "01 Inbox").mkdir(parents=True)
    (vault / "04 Resources" / "policy.md").write_text(
        "---\ntags:\n  - product\n---\n# Availability\nA reserved book remains available for"
        " forty-eight hours. See [[Glossary]].",
        encoding="utf-8",
    )
    (vault / "Glossary.md").write_text(
        "# Glossary\n**Reserved** — held for a reader.", encoding="utf-8"
    )
    (vault / "01 Inbox" / "scratch.md").write_text("unprocessed thought", encoding="utf-8")
    config = project / "kiln" / "project" / "knowledge.json"
    config.write_text(
        json.dumps({"version": 2, "vault": str(vault), "exclude": ["01 Inbox"]}),
        encoding="utf-8",
    )

    synced = command_runner.run(
        console_script("kiln"),
        "knowledge",
        "--working-dir",
        project,
        "sync",
        "--json",
        cwd=REPO_ROOT,
    )
    searched = command_runner.run(
        console_script("kiln"),
        "knowledge",
        "--working-dir",
        project,
        "search",
        "reserved book",
        "--json",
        cwd=REPO_ROOT,
    )
    shown = command_runner.run(
        console_script("kiln"),
        "knowledge",
        "--working-dir",
        project,
        "show",
        json.loads(searched.stdout)[0]["document_id"],
        "--json",
        cwd=REPO_ROOT,
    )

    assert synced.returncode == searched.returncode == shown.returncode == 0
    assert json.loads(synced.stdout)["updated"] == 2, "the excluded inbox note is not indexed"
    result = json.loads(searched.stdout)[0]
    assert result["source_id"] == "vault"
    assert result["heading"] == "Availability"
    assert result["path"] == "04 Resources/policy.md"
    document = json.loads(shown.stdout)
    assert document["related"] == ["Glossary.md"], "the [[wikilink]] resolves to the note"
    assert (project / ".kiln" / "knowledge.db").is_file()
