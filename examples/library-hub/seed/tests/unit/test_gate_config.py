"""Guard against gate configuration being silently weakened by regeneration.

`pyproject.toml` is rewritten by agents every run, so every threshold and rule set
configured there is temporary unless something asserts it. Each gate below has been
lost at least once across prior runs and reinstated by hand; these tests are what
make the next loss a red build instead of a quiet one.

Where a tool can be asked what it will actually do, ask the tool — a setting placed
in the wrong section parses fine and is ignored at runtime, so reading the TOML back
only proves spelling. `test_coverage_fail_under` is the worked example: an earlier
run put `fail_under` under `[tool.coverage.run]`, where coverage.py ignores it, and
a TOML-parsing assertion passed while the floor was 0.

Scaffolded by Kiln, not authored per project. Extend it when a gate is added; do not
relax a threshold here to make a build pass — fix the code, or change the gate
deliberately and say so in the handoff (see constitution/engineering.md).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

#: This file is scaffolded before the project exists, so on the first cycle there is no
#: pyproject.toml to assert against. Skip rather than fail: a red suite the coder did not
#: cause is a distraction it will try to fix. Every gate below re-arms the moment the file
#: appears.
requires_pyproject = pytest.mark.skipif(
    not PYPROJECT.is_file(),
    reason="no pyproject.toml yet - gate thresholds are asserted once the project exists",
)


def _pyproject() -> dict[str, Any]:
    """Parse pyproject.toml from the project root."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _tool(name: str) -> dict[str, Any]:
    """Return the [tool.<name>] table, or an empty dict if absent."""
    section: Any = _pyproject().get("tool", {})
    for part in name.split("."):
        section = section.get(part, {})
    return section


@requires_pyproject
def test_coverage_fail_under_90() -> None:
    """Coverage's own resolved fail_under must be >= 90.

    Asks coverage.py rather than reading the TOML: `fail_under` is only honoured in
    `[tool.coverage.report]`, and the same key under `[tool.coverage.run]` is silently
    discarded.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import coverage; print(coverage.Coverage().config.fail_under)"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )
    fail_under = float(result.stdout.strip() or 0)
    assert fail_under >= 90, (
        f"coverage's resolved fail_under is {fail_under}, expected >= 90. "
        "It belongs in [tool.coverage.report]; under [tool.coverage.run] it is ignored."
    )


@requires_pyproject
def test_coverage_omit_is_empty() -> None:
    """No file may be omitted from coverage.

    An `omit` entry removes a module from the denominator instead of testing it. The
    acceptance suite runs against real containers and covers the infrastructure
    adapters, so they need no exemption.
    """
    omit = _tool("coverage.run").get("omit", [])
    assert not omit, (
        f"[tool.coverage.run] omit is {omit}, expected empty. "
        "Omitting a file raises the percentage without testing anything."
    )


@requires_pyproject
def test_mypy_strict_and_files() -> None:
    """mypy must run strict, over both contexts, with no arguments."""
    mypy = _tool("mypy")
    assert mypy.get("strict") is True, (
        f"[tool.mypy] strict is {mypy.get('strict')!r}, expected True"
    )
    files = mypy.get("files", [])
    assert "catalog" in files and "loans" in files, (
        f"[tool.mypy] files is {files}, expected both catalog and loans so bare `mypy` works"
    )


@requires_pyproject
def test_interrogate_fail_under_90() -> None:
    """Docstring coverage must be gated at >= 90."""
    fail_under = _tool("interrogate").get("fail-under", 0)
    assert fail_under >= 90, (
        f"[tool.interrogate] fail-under is {fail_under}, expected >= 90"
    )


def test_seed_fixture_matches_the_requirements() -> None:
    """The pinned catalog seed values must not be substituted.

    The requirements choose these values so a filter term matches at most one book. Swapping in
    others — "Science Fiction" for "Sci-Fi", say — re-opens the substring ambiguity they were
    chosen to close, and it surfaces only as an acceptance failure the coder cannot fix, because
    the feature file is specifier-owned.
    """
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "catalog_seed.py"
    if not fixture.is_file():
        pytest.skip("seed fixture not created yet")

    # Load by path rather than by import: the fixture must be checkable however pytest was
    # invoked, and its own docstring legitimately names the values it warns against.
    spec = importlib.util.spec_from_file_location("_catalog_seed", fixture)
    assert spec is not None and spec.loader is not None, f"could not load {fixture}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    actual = {(b.isbn, b.title, b.author, b.genre) for b in module.SEED_BOOKS}
    expected = {
        ("978-0-20-163361-0", "Dune", "Frank Herbert", "Sci-Fi"),
        ("978-0-13-468599-1", "Refactoring", "Martin Fowler", "Software"),
        ("978-3-16-148410-0", "The Hobbit", "J.R.R. Tolkien", "Fantasy"),
    }
    assert actual == expected, (
        f"seed books are {sorted(actual)}, expected {sorted(expected)}. These values are pinned "
        "in the requirements so a filter term matches at most one book; substituting others "
        "re-opens the substring ambiguity they were chosen to close."
    )


@requires_pyproject
def test_ruff_select_is_not_narrowed() -> None:
    """The lint rule set must be pinned and must not shrink.

    Omitting `select` falls back to ruff's defaults (E4, E7, E9, F), which drops import
    ordering, pyupgrade and bugbear. The gate still prints "All checks passed!" while
    checking substantially less.
    """
    required = {"E", "F", "I", "UP", "B"}
    select = set(_tool("ruff.lint").get("select", []))
    missing = required - select
    assert not missing, (
        f"[tool.ruff.lint] select is {sorted(select) or 'unset (ruff defaults)'}, "
        f"missing {sorted(missing)}. Expected at least {sorted(required)}."
    )


@requires_pyproject
def test_import_contracts_cover_layers_and_context_isolation() -> None:
    """Both dependency direction and bounded-context isolation must be enforced.

    Four layering contracts prove each context is internally well-ordered; without the
    two isolation contracts nothing stops one context importing the other's domain
    directly, which is the failure the architecture exists to prevent.
    """
    contracts = _pyproject().get("tool", {}).get("importlinter", {}).get("contracts", [])
    pairs = {
        (tuple(c.get("source_modules", [])), tuple(c.get("forbidden_modules", [])))
        for c in contracts
    }
    flat = {(s, f) for sources, forbidden in pairs for s in sources for f in forbidden}

    for context, other in (("catalog", "loans"), ("loans", "catalog")):
        assert (f"{context}.domain", f"{context}.infrastructure") in flat, (
            f"missing contract: {context}.domain must not import {context}.infrastructure"
        )
        assert (f"{context}.application", f"{context}.infrastructure") in flat, (
            f"missing contract: {context}.application must not import {context}.infrastructure"
        )
        assert (context, other) in flat, (
            f"missing contract: {context} must not import {other}: "
            "the bounded contexts communicate by events, never by import"
        )
