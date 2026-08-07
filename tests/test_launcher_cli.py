"""
Scaffolding and the CLI entry point.

Scaffolding runs against a real framework tree and a real git repo — it is almost entirely
file copying, so a mocked filesystem would test nothing worth knowing.
"""

from __future__ import annotations

import json

import pytest
from launcher import cli, scaffold
from launcher.paths import KilnPaths

pytestmark = pytest.mark.integration


@pytest.fixture
def framework(tmp_path):
    """A minimal but structurally real framework checkout."""
    root = tmp_path / "framework"
    bundled = root / "kiln"

    constitution = bundled / "project" / "constitution"
    constitution.mkdir(parents=True)
    for name in scaffold.CONSTITUTION_FILES:
        (constitution / name).write_text(f"# {name}\n", encoding="utf-8")
    (bundled / "project" / "constitution.md").write_text("# Constitution\n", encoding="utf-8")

    roles = bundled / "project" / "roles"
    roles.mkdir(parents=True)
    for role in ("coder", "specifier"):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")

    skill = bundled / "project" / "skills" / "kiln-handoff"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: kiln-handoff\n---\n", encoding="utf-8")

    (bundled / ".claude").mkdir(parents=True)
    (bundled / ".claude" / "settings.json").write_text('{"x": 1}', encoding="utf-8")

    example = root / "examples" / "demo"
    (example / "kiln" / "project" / "constitution").mkdir(parents=True)
    (example / "README.md").write_text("# Demo Brief\n", encoding="utf-8")
    (example / "kiln" / "project" / "constitution" / "project.md").write_text(
        "# Demo project rules\n", encoding="utf-8"
    )
    return root


class TestScaffold:
    def test_creates_the_expected_tree(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert paths.constitution_dir.is_dir()
        assert paths.roles_dir.is_dir()
        assert paths.skills_dir.is_dir()
        assert paths.state_dir.is_dir()

    def test_copies_the_constitution_and_roles(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert (paths.constitution_dir / "workflow.md").is_file()
        assert (paths.kiln_project_dir / "constitution.md").is_file()
        assert (paths.roles_dir / "coder.md").is_file()

    def test_copies_skills_as_real_directories(self, tmp_path, framework):
        # Copied, not linked: they become the user's own editable content.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert (paths.skills_dir / "kiln-handoff" / "SKILL.md").is_file()

    def test_stale_skill_copies_are_replaced(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        stale = paths.skills_dir / "kiln-handoff" / "STALE.md"
        stale.write_text("old", encoding="utf-8")

        scaffold.scaffold(target, framework)

        assert not stale.exists(), "a prior partial copy must not linger"

    def test_initial_mcp_has_db_only(self, tmp_path, framework):
        # kiln-channel is role-scoped and no role exists yet.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
        assert set(config["mcpServers"]) == {"kiln-db"}

    def test_creates_the_message_database(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert (target / ".kiln" / "messages.db").is_file()

    def test_database_has_the_real_schema(self, tmp_path, framework):
        import sqlite3

        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        with sqlite3.connect(target / ".kiln" / "messages.db") as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert {"sender", "target", "status", "content", "branch"} <= columns

    def test_initialises_git_without_committing(self, tmp_path, framework):
        # The scaffold cannot know what else belongs in the project's first commit.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert (target / ".git").exists()
        from launcher import workspace

        assert workspace.run_git(["rev-parse", "HEAD"], target).returncode != 0

    def test_no_git_flag_is_honoured(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework, no_git=True)
        assert not (target / ".git").exists()

    def test_gitignore_is_written(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert ".kiln" in (target / ".gitignore").read_text(encoding="utf-8")

    def test_example_brief_and_overrides_are_applied(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework, example="demo")
        assert "Demo Brief" in (target / "README.md").read_text(encoding="utf-8")
        project_md = target / "kiln" / "project" / "constitution" / "project.md"
        assert "Demo project rules" in project_md.read_text(encoding="utf-8")

    def test_unknown_example_warns_without_failing(self, tmp_path, framework):
        result = scaffold.scaffold(tmp_path / "proj", framework, example="nope")
        assert any("not found" in warning for warning in result.warnings)

    def test_is_idempotent(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        scaffold.scaffold(target, framework)
        assert (target / "kiln" / "project" / "roles" / "coder.md").is_file()

    def test_missing_framework_content_is_fatal(self, tmp_path):
        with pytest.raises(scaffold.ScaffoldError, match="framework content not found"):
            scaffold.scaffold(tmp_path / "proj", tmp_path / "empty")


class TestCliParsing:
    def test_init_subcommand_is_recognised(self):
        args = cli.build_parser().parse_args(["init", "--working-dir", "x"])
        assert args.command == "init"

    def test_powershell_style_flags_are_accepted(self):
        # The shims forward arguments through unchanged.
        args = cli.build_parser().parse_args(["-WorkingDir", "x", "-Profile", "compact"])
        assert args.working_dir == "x"
        assert args.profile == "compact"

    def test_unknown_positional_is_rejected(self, caplog):
        assert cli.main(["bogus"]) == 1

    def test_stop_and_list_flags(self):
        assert cli.build_parser().parse_args(["--stop"]).stop is True
        assert cli.build_parser().parse_args(["--list-profiles"]).list_profiles is True

    def test_defaults_to_the_current_directory(self):
        assert cli.build_parser().parse_args([]).working_dir == "."


class TestCliErrors:
    def test_missing_working_directory_is_reported(self, tmp_path):
        assert cli.main(["--working-dir", str(tmp_path / "absent")]) == 1

    def test_missing_profile_is_reported(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(
            cli, "resolve_framework_root", lambda *a, **k: tmp_path / "no-framework"
        )
        assert cli.main(["--working-dir", str(project)]) == 1


class TestUnscaffoldedProject:
    """
    Regression: launching a project whose kiln/project/ is missing.

    Found by a live run against a real project whose kiln/ was never committed. It failed
    with a raw Python traceback from deep inside worker-file generation, naming only a path.
    """

    def test_missing_scaffolding_is_reported_with_a_remedy(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        with pytest.raises(TemplateError, match="not a scaffolded Kiln project"):
            check_project_scaffolding(paths)

    def test_error_names_the_init_command(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        with pytest.raises(TemplateError, match="kiln init"):
            check_project_scaffolding(paths)

    def test_missing_workflow_is_fatal(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        with pytest.raises(TemplateError, match="handoff routing"):
            check_project_scaffolding(paths)

    def test_workflow_alone_is_enough_to_proceed(self, tmp_path):
        from launcher.templates import check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        paths.workflow_md.write_text("# Workflow\n", encoding="utf-8")
        check_project_scaffolding(paths)  # must not raise

    def test_optional_sections_degrade_instead_of_aborting(self, tmp_path):
        from launcher.templates import read_constitution

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        assert read_constitution(paths, "project") == ""
        assert read_constitution(paths, "engineering") == ""

    def test_cli_reports_cleanly_instead_of_a_traceback(self, tmp_path, framework, monkeypatch):
        # The launcher must never surface a Python traceback for a user-fixable condition.
        monkeypatch.setattr(cli, "resolve_framework_root", lambda *a, **k: framework)
        project = tmp_path / "unscaffolded"
        project.mkdir()
        (project / "kiln.profiles.json").write_text(
            json.dumps({"profiles": {"p": {"terminals": [{"role": "coder"}]}}, "default": "p"}),
            encoding="utf-8",
        )
        assert cli.main(["--working-dir", str(project), "--terminal", "none"]) == 1


class TestCliInit:
    def test_scaffolds_through_the_cli(self, tmp_path, framework, monkeypatch):
        monkeypatch.setattr(cli, "resolve_framework_root", lambda *a, **k: framework)
        target = tmp_path / "viacli"
        assert cli.main(["init", "--working-dir", str(target)]) == 0
        assert (target / "kiln" / "project" / "constitution" / "workflow.md").is_file()
