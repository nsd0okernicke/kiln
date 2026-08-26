"""Acceptance scenario: public commands build and query project knowledge."""

import json

from conftest import REPO_ROOT, console_script


def test_catalogs_syncs_and_retrieves_project_knowledge(tmp_path, command_runner):
    project = tmp_path / "knowledge-project"
    initialized = command_runner.run(console_script("kiln"), "init", project, cwd=REPO_ROOT)
    assert initialized.returncode == 0
    docs = project / "docs"
    docs.mkdir()
    (docs / "policy.md").write_text(
        "# Availability\nA reserved book remains available for forty-eight hours.",
        encoding="utf-8",
    )

    added = command_runner.run(
        console_script("kiln"),
        "knowledge",
        "--working-dir",
        project,
        "add",
        "docs/policy.md",
        "--id",
        "availability",
        "--title",
        "Availability policy",
        cwd=REPO_ROOT,
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

    assert added.returncode == synced.returncode == searched.returncode == 0
    assert json.loads(synced.stdout)["updated"] == 1
    result = json.loads(searched.stdout)[0]
    assert result["source_id"] == "availability"
    assert result["heading"] == "Availability"
    assert result["path"] == "docs/policy.md"
    assert (project / ".kiln" / "knowledge.db").is_file()
