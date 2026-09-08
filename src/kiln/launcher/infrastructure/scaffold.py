"""
Project scaffolding — `kiln init`.

Ports New-KilnInitScaffold and its Copy-KilnInit* / Initialize-KilnInit* helpers.

Everything under `src/kiln/resources/project/` is copied to the user's editable
`kiln/project/` directory. The remaining package resources stay framework-owned.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kiln.scheduler.infrastructure.vcs import git as git_ops

from ..domain.paths import KilnPaths
from . import workspace

log = logging.getLogger(__name__)

#: Every bundled constitution file that a scaffolded project gets. This is a
#: literal list rather than a directory walk so a stray file cannot become constitution by
#: accident -- but it must stay complete, or a scaffolded project silently lacks a rule
#: source. The integration docs-consistency test pins the list against the bundled directory.
CONSTITUTION_FILES = (
    "engineering.md",
    "workflow.md",
    "project.md",
)

#: Project-level documents copied beside constitution.md rather than into constitution/.
#: `skill-orchestration.md` lives here because nothing loads it: a worker's prompt is its role
#: file plus project.md and engineering.md, so a "constitution" document defining gate
#: ownership reached none of the roles that run gates. The binding copy of that ownership is
#: in roles/*.md; this is the human-facing overview and is honest about being one.
REFERENCE_FILES = ("skill-orchestration.md",)

#: Path to the CI workflow template, relative to the scaffold resources directory.
CI_WORKFLOW_SOURCE = "github/workflows/ci.yml"

#: Target path for the CI workflow, relative to the project root.
CI_WORKFLOW_TARGET = ".github/workflows/ci.yml"


class ScaffoldError(Exception):
    """Scaffolding could not complete."""


@dataclass
class ScaffoldResult:
    target: Path
    created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.created.append(message)
        log.info("  %s", message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning("  %s", message)


def create_directories(paths: KilnPaths, result: ScaffoldResult) -> None:
    for directory in (
        paths.constitution_dir,
        paths.roles_dir,
        paths.skills_dir,
        paths.state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    result.note("created directory structure")


def copy_constitution(paths: KilnPaths, result: ScaffoldResult) -> None:
    source_dir = paths.scaffold_resources_dir / "constitution"
    copied = 0
    for name in CONSTITUTION_FILES:
        source = source_dir / name
        if source.is_file():
            workspace.copy_template_file(source, paths.constitution_dir / name)
            copied += 1

    # The framework's own constitution.md is copied rather than synthesised, so there is one
    # source of truth for it like the other bundled scaffold resources.
    header = paths.scaffold_resources_dir / "constitution.md"
    if header.is_file():
        workspace.copy_template_file(header, paths.kiln_project_dir / "constitution.md")
        copied += 1

    result.note(f"copied {copied} constitution file(s)")


def copy_reference_docs(paths: KilnPaths, result: ScaffoldResult) -> None:
    """Copy the non-binding project references that sit beside constitution.md."""
    copied = 0
    for name in REFERENCE_FILES:
        source = paths.scaffold_resources_dir / name
        if source.is_file():
            workspace.copy_template_file(source, paths.kiln_project_dir / name)
            copied += 1
    if copied:
        result.note(f"copied {copied} reference document(s)")


def copy_roles(paths: KilnPaths, result: ScaffoldResult) -> None:
    source_dir = paths.scaffold_resources_dir / "roles"
    if not source_dir.is_dir():
        result.warn("no framework roles directory found")
        return
    count = 0
    for source in source_dir.glob("*.md"):
        workspace.copy_template_file(source, paths.roles_dir / source.name)
        count += 1
    result.note(f"copied {count} role file(s)")


def copy_skills(paths: KilnPaths, result: ScaffoldResult) -> None:
    source_dir = paths.scaffold_resources_dir / "skills"
    if not source_dir.is_dir():
        return
    count = 0
    for source in source_dir.iterdir():
        if not source.is_dir():
            continue
        # Remove any prior copy first so a stale or half-copied skill cannot shadow the
        # fresh one.
        destination = paths.skills_dir / source.name
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        workspace.copy_template_tree(source, destination)
        count += 1
    result.note(f"copied {count} skill(s)")


def copy_ci_workflow(paths: KilnPaths, result: ScaffoldResult, example: str = "") -> None:
    """
    Copy the CI workflow template into the project's `.github/workflows/`.

    When an example is specified, prefers the example's own CI workflow over
    the generic language-agnostic template. An example at
    ``examples/<name>/.github/workflows/ci.yml`` replaces the stubbed default
    with project-specific gate commands.

    `.github/workflows/ci.yml` is deliberately tracked (not gitignored) so a role
    cannot change what CI runs — CI reads this file from the shared branch.
    """
    # Check for an example-specific CI workflow first
    if example:
        example_source = (
            paths.framework_root / "examples" / example / ".github" / "workflows" / "ci.yml"
        )
        if example_source.is_file():
            target = paths.project_root / CI_WORKFLOW_TARGET
            target.parent.mkdir(parents=True, exist_ok=True)
            workspace.copy_template_file(example_source, target)
            result.note(f"created .github/workflows/ci.yml (from {example} example)")
            return

    source = paths.scaffold_resources_dir / CI_WORKFLOW_SOURCE
    if not source.is_file():
        result.warn(f"CI workflow template not found at {source}")
        return
    target = paths.project_root / CI_WORKFLOW_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace.copy_template_file(source, target)
    result.note("created .github/workflows/ci.yml")


#: Extension `.githubignore` entries for tracked CI workflows.
_CI_WORKFLOW_GITIGNORE = (
    "# CI workflow files must be tracked (not gitignored) so CI runs from committed code",
    "!.github/workflows/",
)


def copy_knowledge_catalog(paths: KilnPaths, result: ScaffoldResult) -> None:
    source = paths.scaffold_resources_dir / "knowledge.json"
    if source.is_file() and not paths.knowledge_manifest.exists():
        workspace.copy_template_file(source, paths.knowledge_manifest)
        result.note("created knowledge source catalog")


def write_initial_mcp_json(paths: KilnPaths, result: ScaffoldResult) -> None:
    """
    kiln-db only at scaffold time.

    kiln-channel is role-scoped, and no role exists yet — the first real launch replaces
    this with the full config once roles are known.
    """
    config = {
        "mcpServers": {"kiln-db": {"command": "npx", "args": ["mcp-sqlite", str(paths.db_path)]}}
    }
    (paths.project_root / ".mcp.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    result.note("created .mcp.json")


def write_claude_settings(paths: KilnPaths, result: ScaffoldResult) -> None:
    template = paths.claude_settings_template
    if not template.is_file():
        result.warn(f"Claude settings template not found at {template}")
        return
    target = paths.project_root / ".claude"
    target.mkdir(parents=True, exist_ok=True)
    workspace.copy_template_file(template, target / "settings.json")
    workspace.write_directory_gitignore(target)
    result.note("created .claude/settings.json")


def copy_example(paths: KilnPaths, example: str, result: ScaffoldResult) -> None:
    """
    Seed the project from `examples/<name>/`.

    Every file the example overrides is copied, not a hardcoded list, so adding a new
    example needs no change here.

    The steps run in order and are independent: each copies what the example provides and
    returns a note, or None when the example provides nothing of that kind. Adding a new
    kind of seeded file is a new step in `_EXAMPLE_STEPS`, not another branch here.
    """
    if not example:
        return
    example_dir = _resolve_example_dir(paths, example)
    for step in _EXAMPLE_STEPS:
        note = step(example_dir, paths, example)
        if note:
            result.note(note)


def _resolve_example_dir(paths: KilnPaths, example: str) -> Path:
    """The example's directory, or a ScaffoldError naming the ones that do exist."""
    example_dir = paths.framework_root / "examples" / example
    if example_dir.is_dir():
        return example_dir
    known = sorted(p.name for p in (paths.framework_root / "examples").iterdir() if p.is_dir())
    raise ScaffoldError(
        f"example {example!r} not found under examples/. Available examples: {', '.join(known)}"
    )


def _copy_example_readme(example_dir: Path, paths: KilnPaths, example: str) -> str:
    """The example's brief becomes the project's README."""
    readme = example_dir / "README.md"
    if not readme.is_file():
        return ""
    workspace.copy_template_file(readme, paths.project_root / "README.md")
    return f"copied example brief from {example}"


def _copy_example_constitution(example_dir: Path, paths: KilnPaths, example: str) -> str:
    """Constitution files the example overrides, replacing the framework defaults."""
    overrides = example_dir / "kiln" / "project" / "constitution"
    count = _copy_constitution_overrides(overrides, paths.constitution_dir)
    return f"applied {count} example constitution override(s)" if count else ""


def _copy_example_metrics(example_dir: Path, paths: KilnPaths, example: str) -> str:
    """The example's test-metrics configuration, read by the cockpit."""
    metrics = example_dir / "test-metrics.json"
    if not metrics.is_file():
        return ""
    workspace.copy_template_file(metrics, paths.state_dir / "test-metrics.json")
    return f"configured test metrics for {example}"


#: Asset file types an example may drop at the project root — briefs, diagrams, sprite packs.
#: README.md is excluded because `_copy_example_readme` has already placed it.
_ASSET_SUFFIXES = (
    ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".md", ".txt", ".pdf",
)


def _is_example_asset(path: Path) -> bool:
    """A loose file at the example root that belongs in the project root."""
    return path.is_file() and path.name != "README.md" and path.suffix in _ASSET_SUFFIXES


def _copy_example_assets(example_dir: Path, paths: KilnPaths, example: str) -> str:
    """Loose asset files at the example root, copied to the project root."""
    assets = [p for p in example_dir.iterdir() if _is_example_asset(p)]
    for asset in assets:
        workspace.copy_template_file(asset, paths.project_root / asset.name)
    return f"copied {len(assets)} example asset file(s)" if assets else ""


def _copy_example_seed(example_dir: Path, paths: KilnPaths, example: str) -> str:
    """Gate artifacts the scaffold ships so a regenerating agent cannot drop them."""
    seeded = _copy_example_seed_files(example_dir, paths.project_root)
    return f"seeded {seeded} gate artifact(s) from {example}" if seeded else ""


#: Seeding steps, in order. Each returns a note for the scaffold result, or "" for nothing done.
_EXAMPLE_STEPS = (
    _copy_example_readme,
    _copy_example_constitution,
    _copy_example_metrics,
    _copy_example_assets,
    _copy_example_seed,
)


#: Directory in an example whose contents are copied into the project root verbatim,
#: preserving relative paths — `examples/<name>/seed/tests/x.py` becomes `<project>/tests/x.py`.
#:
#: These carry the gates themselves rather than advice about them, so the scaffold ships them
#: and a regenerating agent cannot quietly drop them. Every file placed here has been lost at
#: least once by living only in the generated project: a rule in the constitution survives a
#: run, a file the agents rewrite does not.
#:
#: A directory rather than a fixed list because the paths are language-specific — a Python
#: example seeds `tests/unit/test_gate_config.py`, a Java one
#: `catalog-service/src/test/java/.../GateConfigTest.java`.
EXAMPLE_SEED_DIR = "seed"


def _copy_example_seed_files(example_dir: Path, project_root: Path) -> int:
    """Copy everything under the example's seed/ directory, preserving relative paths."""
    seed_root = example_dir / EXAMPLE_SEED_DIR
    if not seed_root.is_dir():
        return 0
    copied = 0
    for source in sorted(seed_root.rglob("*")):
        if not source.is_file():
            continue
        target = project_root / source.relative_to(seed_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        workspace.copy_template_file(source, target)
        copied += 1
    return copied


def _copy_constitution_overrides(source_dir: Path, target_dir: Path) -> int:
    if not source_dir.is_dir():
        return 0
    sources = [source for source in source_dir.iterdir() if source.is_file()]
    for source in sources:
        workspace.copy_template_file(source, target_dir / source.name)
    return len(sources)


def initialize_database(paths: KilnPaths, result: ScaffoldResult) -> None:
    """Create the message queue using the scheduler package's own schema."""
    sys.path.insert(0, str(paths.python_package_root))
    from kiln.scheduler.infrastructure.persistence.db import ensure_schema

    ensure_schema(paths.db_path)
    result.note("initialised message database")


def initialize_git(paths: KilnPaths, result: ScaffoldResult) -> None:
    """
    Set up git and write `.gitignore`/`.gitattributes`, but leave the first commit to the user.

    Deliberately no initial commit: the scaffold has no idea what else belongs in the
    project's first commit, and the launcher commits .gitignore itself when needed.

    Both files are written here as well as at launch. Writing them only at launch left `init`
    producing a project whose first commit lacked them, so the user's own opening commit was
    immediately followed by an unexplained modification the first time they ran `kiln`.
    """
    if (paths.project_root / ".git").exists():
        _write_git_metadata(paths)
        result.note("existing git repository; .gitignore updated")
        return

    workspace.run_git(["init"], paths.project_root, check=True)
    workspace.run_git(["branch", "-M", "main"], paths.project_root)
    _write_git_metadata(paths)
    result.note("initialised git repository (first commit left to you)")


def _write_git_metadata(paths: KilnPaths) -> None:
    """The ignore rules, the merge attributes, and the local-only copy of the latter."""
    workspace.ensure_gitignore(paths)
    workspace.ensure_gitattributes(paths)
    # Effective without waiting for the committed file to reach each role's branch --
    # see git_ops.ensure_union_merge.
    git_ops.ensure_union_merge(paths.project_root)


def scaffold(
    target: str | Path,
    framework_root: str | Path,
    example: str = "",
    no_git: bool = False,
) -> ScaffoldResult:
    """Create a complete Kiln project at `target`."""
    project_root = Path(target).expanduser().resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    paths = KilnPaths.create(project_root, framework_root)

    if not paths.scaffold_resources_dir.is_dir():
        raise ScaffoldError(f"framework content not found at {paths.scaffold_resources_dir}")

    result = ScaffoldResult(target=project_root)
    log.info("Scaffolding Kiln project at %s", project_root)

    create_directories(paths, result)
    copy_constitution(paths, result)
    copy_reference_docs(paths, result)
    copy_roles(paths, result)
    copy_skills(paths, result)
    copy_knowledge_catalog(paths, result)
    copy_ci_workflow(paths, result, example=example)
    write_initial_mcp_json(paths, result)
    write_claude_settings(paths, result)
    copy_example(paths, example, result)
    initialize_database(paths, result)
    if not no_git:
        initialize_git(paths, result)

    log.info("Done. Next: cd %s && kiln", project_root)
    return result
