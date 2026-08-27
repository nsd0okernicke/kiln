"""
Tests for the Kiln version resolver.

Covers the four resolution cases from the issue:
- Exact tagged commit  → ``"v0.4.0"``
- Commit after a tag   → ``"v0.4.0-12-gabc1234"``
- Dirty checkout       → ``"v0.4.0-12-gabc1234-dirty"``
- No Git metadata      → ``"unknown"`` (fallback)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiln.launcher.application.version import (
    FALLBACK_VERSION,
    clear_cache,
    resolve_version,
    version_tuple,
)


class TestResolveVersion:
    """``resolve_version`` — the main public function."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        clear_cache()

    def test_unknown_when_git_missing(self, tmp_path: Path) -> None:
        """A directory with no ``.git`` must produce the fallback."""
        v = resolve_version(tmp_path)
        assert v == FALLBACK_VERSION

    def test_unknown_when_no_tags(self, bare_git_repo: Path) -> None:
        """A Git repo with no matching tags must produce the fallback."""
        v = resolve_version(bare_git_repo)
        assert v == FALLBACK_VERSION

    def test_exact_tag(self, tagged_repo: tuple[Path, str]) -> None:
        """An exact tagged commit must return that tag."""
        repo, tag = tagged_repo
        v = resolve_version(repo)
        assert v == tag

    def test_ahead_of_tag(self, ahead_repo: tuple[Path, str]) -> None:
        """A commit after a tag must include commit count and hash."""
        repo, expected = ahead_repo
        v = resolve_version(repo)
        assert v.startswith(expected.rstrip("-dirty"))
        assert "-g" in v


class TestResolveVersionDirty:
    """Dirty-checkout resolution (separate fixture to avoid cross-test side effects)."""

    def test_dirty_marker(self, dirty_repo: tuple[Path, str]) -> None:
        """A dirty checkout must append ``-dirty``."""
        repo, expected = dirty_repo
        v = resolve_version(repo)
        # The version must end with -dirty.
        assert v.endswith("-dirty"), f"expected -dirty suffix in {v!r}"
        # The expected value from the fixture is what resolve_version returned.
        assert v == expected


class TestVersionTuple:
    """``version_tuple`` — parsing and comparison."""

    def test_full_semver(self) -> None:
        assert version_tuple("v0.4.0") == (0, 4, 0)

    def test_two_part(self) -> None:
        assert version_tuple("v0.1") == (0, 1)

    def test_dirty_version(self) -> None:
        assert version_tuple("v0.4.0-12-gabc1234-dirty") == (0, 4, 0)

    def test_dev_version(self) -> None:
        assert version_tuple("v0.4.0-12-gabc1234") == (0, 4, 0)

    def test_fallback(self) -> None:
        assert version_tuple("unknown") == (0,)

    def test_ordering(self) -> None:
        assert version_tuple("v0.4.0") > version_tuple("v0.3.0")
        assert version_tuple("v0.4.0") > version_tuple("v0.4")


# ---------------------------------------------------------------------------
# Fixtures — real Git repos because mocking subprocess always misses details.
# ---------------------------------------------------------------------------


@pytest.fixture
def bare_git_repo(tmp_path: Path) -> Path:
    """A valid Git repository with no tags at all."""
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@kiln")
    _git(repo, "config", "user.name", "Test Kiln")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


@pytest.fixture
def tagged_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository on an exact annotated tag ``v0.4.0``."""
    repo = tmp_path / "tagged"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@kiln")
    _git(repo, "config", "user.name", "Test Kiln")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "tag", "-a", "-m", "release v0.4.0", "v0.4.0")
    return repo, "v0.4.0"


@pytest.fixture
def ahead_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with one commit after the tag."""
    repo = tmp_path / "ahead"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@kiln")
    _git(repo, "config", "user.name", "Test Kiln")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "tag", "-a", "-m", "release v0.4.0", "v0.4.0")
    _git(repo, "commit", "--allow-empty", "-m", "second commit")
    # Return the expected prefix (we'll have v0.4.0-1-g<hash>).
    return repo, "v0.4.0-1"


@pytest.fixture
def dirty_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with uncommitted changes after a tag."""
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@kiln")
    _git(repo, "config", "user.name", "Test Kiln")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "tag", "-a", "-m", "release v0.4.0", "v0.4.0")
    _git(repo, "commit", "--allow-empty", "-m", "second commit")
    # Modify a tracked file so the working tree becomes dirty.
    (repo / "dirty.txt").write_text("dirty")
    _git(repo, "add", "dirty.txt")
    _git(repo, "commit", "-m", "add dirty.txt")
    (repo / "dirty.txt").write_text("modified")
    v = resolve_version(repo)
    return repo, v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
