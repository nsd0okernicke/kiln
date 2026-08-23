"""
Project scaffolding — `kiln init`.

Ports New-KilnInitScaffold and its Copy-KilnInit* / Initialize-KilnInit* helpers.

Everything under `kiln/project/` is *copied* rather than referenced: it becomes the user's
own editable constitution, roles and skills. Only `src/` stays framework-owned
and is read in place.
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

#: Every file under kiln/project/constitution/ that a scaffolded project gets. This is a
#: literal list rather than a directory walk so a stray file cannot become constitution by
#: accident -- but it must stay complete: `skill-orchestration.md` was missing from it, so no
#: scaffolded project had the document that defines which role owns which quality gate.
#: The integration docs-consistency test pins the list against the bundled directory.
CONSTITUTION_FILES = (
    "engineering.md",
    "workflow.md",
    "project.md",
    "skill-orchestration.md",
)


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
    source_dir = paths.bundled_dir / "project" / "constitution"
    copied = 0
    for name in CONSTITUTION_FILES:
        source = source_dir / name
        if source.is_file():
            workspace.copy_template_file(source, paths.constitution_dir / name)
            copied += 1

    # The framework's own constitution.md is copied rather than synthesised, so there is one
    # source of truth for it like everything else under kiln/project/.
    header = paths.bundled_dir / "project" / "constitution.md"
    if header.is_file():
        workspace.copy_template_file(header, paths.kiln_project_dir / "constitution.md")
        copied += 1

    result.note(f"copied {copied} constitution file(s)")


def copy_roles(paths: KilnPaths, result: ScaffoldResult) -> None:
    source_dir = paths.bundled_dir / "project" / "roles"
    if not source_dir.is_dir():
        result.warn("no framework roles directory found")
        return
    count = 0
    for source in source_dir.glob("*.md"):
        workspace.copy_template_file(source, paths.roles_dir / source.name)
        count += 1
    result.note(f"copied {count} role file(s)")


def copy_skills(paths: KilnPaths, result: ScaffoldResult) -> None:
    source_dir = paths.bundled_dir / "project" / "skills"
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
    """
    if not example:
        return
    example_dir = paths.framework_root / "examples" / example
    if not example_dir.is_dir():
        result.warn(f"example {example!r} not found under examples/; skipping")
        return

    readme = example_dir / "README.md"
    if readme.is_file():
        workspace.copy_template_file(readme, paths.project_root / "README.md")
        result.note(f"copied example brief from {example}")

    overrides = example_dir / "kiln" / "project" / "constitution"
    if overrides.is_dir():
        count = 0
        for source in overrides.iterdir():
            if source.is_file():
                workspace.copy_template_file(source, paths.constitution_dir / source.name)
                count += 1
        if count:
            result.note(f"applied {count} example constitution override(s)")


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

    if not paths.bundled_dir.is_dir():
        raise ScaffoldError(f"framework content not found at {paths.bundled_dir}")

    result = ScaffoldResult(target=project_root)
    log.info("Scaffolding Kiln project at %s", project_root)

    create_directories(paths, result)
    copy_constitution(paths, result)
    copy_roles(paths, result)
    copy_skills(paths, result)
    write_initial_mcp_json(paths, result)
    write_claude_settings(paths, result)
    copy_example(paths, example, result)
    initialize_database(paths, result)
    if not no_git:
        initialize_git(paths, result)

    log.info("Done. Next: cd %s && kiln", project_root)
    return result
