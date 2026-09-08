"""
What `kiln init` actually puts on disk, against real directories.

The acceptance tier runs `kiln init` as a subprocess, which proves the command works but
observes it only through the files it leaves behind. These tests call the scaffold steps
directly, so the branches that matter when a template or an example is *incomplete* -- the
usual state of a half-added example -- are reachable.

The framework tree is built per test rather than pointed at the real `src/kiln/resources`:
a test asserting against the shipped resources would fail every time one is added, which is
the opposite of what it should notice.
"""

from __future__ import annotations

import pytest

from kiln.launcher.domain.paths import KilnPaths
from kiln.launcher.infrastructure import scaffold

pytestmark = pytest.mark.integration


@pytest.fixture
def framework(tmp_path):
    """A framework tree with nothing in it -- each test adds only what it is about."""
    root = tmp_path / "framework"
    (root / "src" / "kiln" / "resources" / "project").mkdir(parents=True)
    (root / "examples").mkdir(parents=True)
    return root


@pytest.fixture
def paths(tmp_path, framework):
    project = tmp_path / "project"
    project.mkdir()
    return KilnPaths.create(project, framework)


@pytest.fixture
def result(paths):
    return scaffold.ScaffoldResult(target=paths.project_root)


@pytest.fixture
def example(framework):
    """Build one example directory; returns its path so a test can fill it in."""

    def _make(name="library-hub"):
        directory = framework / "examples" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    return _make


def write(path, text="content\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestExampleSeedFiles:
    """
    `examples/<name>/seed/` -- the gate artifacts the scaffold ships rather than describes.

    Every file under it has been lost at least once by living only in the generated project.
    A rule in the constitution survives a regeneration; a test file the agents rewrite does
    not, so these are copied in and the layout is preserved verbatim.
    """

    def test_the_tree_shape_is_preserved_not_flattened(self, paths, result, example):
        # The seeded file has to land where the language test runner looks for it. A flat
        # copy would put a Java test beside the POM, where nothing would ever run it.
        directory = example()
        write(directory / "seed" / "tests" / "unit" / "test_gate_config.py", "assert True\n")

        scaffold.copy_example(paths, "library-hub", result)

        seeded = paths.project_root / "tests" / "unit" / "test_gate_config.py"
        assert seeded.read_text(encoding="utf-8") == "assert True\n"

    def test_a_deeply_nested_path_survives_intact(self, paths, result, example):
        # The Java example seeds catalog-service/src/test/java/...; nothing about the depth
        # is Python-specific, and the copier must not assume two levels.
        directory = example()
        deep = "catalog-service/src/test/java/com/libraryhub/GateConfigTest.java"
        write(directory / "seed" / deep, "class GateConfigTest {}\n")

        scaffold.copy_example(paths, "library-hub", result)

        assert (paths.project_root / deep).is_file()

    def test_every_seeded_file_is_counted_in_the_summary(self, paths, result, example):
        directory = example()
        write(directory / "seed" / "a.py")
        write(directory / "seed" / "nested" / "b.py")

        scaffold.copy_example(paths, "library-hub", result)

        assert any("seeded 2 gate artifact(s)" in note for note in result.created)

    def test_an_example_without_a_seed_directory_is_not_an_error(self, paths, result, example):
        # Most examples have none. Reporting "seeded 0" would read as a failure.
        example()

        scaffold.copy_example(paths, "library-hub", result)

        assert not any("seeded" in note for note in result.created)
        assert result.warnings == []


class TestExampleAssets:
    """Loose files at the example root -- a brief, a diagram, a sprite pack."""

    def test_documents_and_images_reach_the_project_root(self, paths, result, example):
        directory = example()
        write(directory / "domain-notes.md")
        write(directory / "schema.png")

        scaffold.copy_example(paths, "library-hub", result)

        assert (paths.project_root / "domain-notes.md").is_file()
        assert (paths.project_root / "schema.png").is_file()

    def test_the_readme_is_not_copied_twice(self, paths, result, example):
        # It is already the project README by the time this step runs; counting it again
        # would report one more asset than exists.
        directory = example()
        write(directory / "README.md", "The brief\n")
        write(directory / "notes.md")

        scaffold.copy_example(paths, "library-hub", result)

        assert (paths.project_root / "README.md").read_text(encoding="utf-8") == "The brief\n"
        assert any("copied 1 example asset file(s)" in note for note in result.created)

    def test_files_the_project_has_no_use_for_are_left_behind(self, paths, result, example):
        # An example may carry its own tooling; only documents and images are project input.
        directory = example()
        write(directory / "build.gradle")
        write(directory / "notes.txt")

        scaffold.copy_example(paths, "library-hub", result)

        assert not (paths.project_root / "build.gradle").exists()
        assert (paths.project_root / "notes.txt").is_file()

    def test_directories_at_the_example_root_are_not_assets(self, paths, result, example):
        directory = example()
        (directory / "seed").mkdir()

        scaffold.copy_example(paths, "library-hub", result)

        assert not (paths.project_root / "seed").exists()


class TestExampleOverrides:
    def test_the_example_constitution_replaces_the_framework_default(self, paths, result, example):
        paths.constitution_dir.mkdir(parents=True)
        write(paths.constitution_dir / "project.md", "framework default\n")
        directory = example()
        write(directory / "kiln" / "project" / "constitution" / "project.md", "the example\n")

        scaffold.copy_example(paths, "library-hub", result)

        written = (paths.constitution_dir / "project.md").read_text(encoding="utf-8")
        assert written == "the example\n"

    def test_the_metrics_config_lands_where_the_cockpit_reads_it(self, paths, result, example):
        paths.state_dir.mkdir(parents=True)
        directory = example()
        write(directory / "test-metrics.json", "{}\n")

        scaffold.copy_example(paths, "library-hub", result)

        assert (paths.state_dir / "test-metrics.json").is_file()

    def test_no_example_named_means_nothing_is_seeded(self, paths, result):
        scaffold.copy_example(paths, "", result)

        assert result.created == []

    def test_an_unknown_example_names_the_ones_that_exist(self, paths, result, example):
        example("library-hub")
        example("library-hub-java")

        with pytest.raises(scaffold.ScaffoldError) as caught:
            scaffold.copy_example(paths, "libary-hub", result)

        assert "library-hub" in str(caught.value) and "library-hub-java" in str(caught.value)


class TestCiWorkflow:
    """
    `.github/workflows/ci.yml` is tracked, not gitignored: CI reads it from the shared
    branch, so a role must not be able to change what CI runs.
    """

    def test_an_example_workflow_wins_over_the_generic_template(self, paths, result, example):
        # The generic one is a stub; the example version names real gate commands.
        write(paths.scaffold_resources_dir / scaffold.CI_WORKFLOW_SOURCE, "generic\n")
        write(example() / ".github" / "workflows" / "ci.yml", "mvn verify\n")

        scaffold.copy_ci_workflow(paths, result, example="library-hub")

        target = paths.project_root / scaffold.CI_WORKFLOW_TARGET
        assert target.read_text(encoding="utf-8") == "mvn verify\n"
        assert any("library-hub example" in note for note in result.created)

    def test_the_generic_template_is_used_when_the_example_has_none(self, paths, result, example):
        write(paths.scaffold_resources_dir / scaffold.CI_WORKFLOW_SOURCE, "generic\n")
        example()

        scaffold.copy_ci_workflow(paths, result, example="library-hub")

        target = paths.project_root / scaffold.CI_WORKFLOW_TARGET
        assert target.read_text(encoding="utf-8") == "generic\n"

    def test_a_missing_template_warns_rather_than_failing_the_scaffold(self, paths, result):
        # Every other step still has value; the project just starts without CI.
        scaffold.copy_ci_workflow(paths, result)

        assert not (paths.project_root / scaffold.CI_WORKFLOW_TARGET).exists()
        assert any("CI workflow template not found" in warning for warning in result.warnings)


class TestFrameworkTemplates:
    def test_missing_roles_are_reported_because_no_swarm_can_run_without_them(self, paths, result):
        scaffold.copy_roles(paths, result)

        assert any("no framework roles directory" in warning for warning in result.warnings)

    def test_reference_documents_are_copied_beside_the_constitution(self, paths, result):
        scaffold.create_directories(paths, result)
        for name in scaffold.REFERENCE_FILES:
            write(paths.scaffold_resources_dir / name)

        scaffold.copy_reference_docs(paths, result)

        assert all((paths.kiln_project_dir / name).is_file() for name in scaffold.REFERENCE_FILES)
        expected = f"copied {len(scaffold.REFERENCE_FILES)} reference document(s)"
        assert any(expected in note for note in result.created)

    def test_no_reference_documents_produces_no_note(self, paths, result):
        scaffold.copy_reference_docs(paths, result)

        assert result.created == []

    def test_skills_are_skipped_silently_when_the_framework_ships_none(self, paths, result):
        scaffold.copy_skills(paths, result)

        assert result.created == [] and result.warnings == []

    def test_the_knowledge_catalog_is_seeded_once_and_never_overwritten(self, paths, result):
        # It is the operator list of sources after the first launch; re-running init must
        # not throw away what they added.
        scaffold.create_directories(paths, result)
        write(paths.scaffold_resources_dir / "knowledge.json", "{}\n")

        scaffold.copy_knowledge_catalog(paths, result)
        paths.knowledge_manifest.write_text("mine\n", encoding="utf-8")
        scaffold.copy_knowledge_catalog(paths, result)

        assert paths.knowledge_manifest.read_text(encoding="utf-8") == "mine\n"

    def test_missing_claude_settings_warn_rather_than_stop(self, paths, result):
        scaffold.write_claude_settings(paths, result)

        assert any("Claude settings template not found" in w for w in result.warnings)
