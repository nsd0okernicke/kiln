"""Acceptance scenario: initialize an example project."""

import sqlite3
from contextlib import closing

from conftest import REPO_ROOT, console_script
from workflow_support import git


def test_initializes_an_example_as_a_launchable_project(tmp_path, command_runner):
    project = tmp_path / "example"
    result = command_runner.run(
        console_script("kiln"),
        "init",
        project,
        "--example",
        "library-hub",
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert (project / "README.md").is_file()
    assert "LibraryHub" in (project / "README.md").read_text(encoding="utf-8")
    assert (project / ".kiln" / "test-metrics.json").is_file()
    assert (project / "kiln" / "project" / "constitution" / "project.md").is_file()
    assert git(command_runner, project, "branch", "--show-current").stdout.strip() == "main"
    assert ".kiln" in (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    with closing(sqlite3.connect(project / ".kiln" / "messages.db")) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    assert {"id", "sender", "target", "status", "branch", "work_item"} <= columns
