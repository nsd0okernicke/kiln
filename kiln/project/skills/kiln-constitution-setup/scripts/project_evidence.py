"""List project files that can support a Kiln constitution without interpreting them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

IGNORED_PARTS = frozenset(
    {
        ".git",
        ".kiln",
        ".worktrees",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "reports",
    }
)
CATEGORIES = {
    "manifests": (
        "pyproject.toml",
        "package.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Cargo.toml",
        "go.mod",
        "*.sln",
        "*.csproj",
    ),
    "tooling": (
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "gradlew",
        "mvnw",
        "Makefile",
        "Dockerfile",
        "compose*.yml",
        "compose*.yaml",
        "tox.ini",
        "pytest.ini",
        "ruff.toml",
        "mypy.ini",
        "tsconfig*.json",
    ),
    "ci": (".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", "Jenkinsfile"),
    "documentation": (
        "README*",
        "CONTRIBUTING*",
        "ARCHITECTURE*",
        "docs/**/*.md",
        "adr/**/*.md",
    ),
    "tests": ("tests/**/*", "test/**/*", "src/test/**/*", "**/*.feature"),
    "source_roots": ("src", "app", "lib", "services", "packages", "modules"),
}


def _allowed(path: Path, root: Path) -> bool:
    return not (set(path.relative_to(root).parts) & IGNORED_PARTS)


def _matches(root: Path, patterns: tuple[str, ...], *, include_dirs: bool = False) -> list[str]:
    found = {
        path.relative_to(root).as_posix()
        for pattern in patterns
        for path in root.glob(pattern)
        if _allowed(path, root) and (path.is_file() or include_dirs)
    }
    return sorted(found)


def inventory(root: Path) -> dict[str, object]:
    resolved = root.resolve()
    categories = {
        name: _matches(resolved, patterns, include_dirs=name == "source_roots")
        for name, patterns in CATEGORIES.items()
    }
    evidence_count = sum(len(paths) for paths in categories.values())
    return {
        "root": str(resolved),
        "evidence_count": evidence_count,
        "suggested_mode": "interview" if evidence_count == 0 else "repository-or-mixed",
        "categories": categories,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", type=Path)
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error(f"project root is not a directory: {args.root}")
    print(json.dumps(inventory(args.root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
