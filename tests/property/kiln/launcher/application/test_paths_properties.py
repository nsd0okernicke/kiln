"""Property-based tests for RunIndex path derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.launcher.domain.paths import RunIndex, slugify

#: ASCII characters that are valid in filesystem paths (excluding the null byte).
VALID_FS_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


_counter: int = 0


def _random_run_id() -> str:
    """Generate a unique run ID with characters that could come from a branch name."""
    import random

    global _counter
    _counter += 1
    prefix = random.choice(
        ["feature", "bugfix", "hotfix", "release", "chore", "dependabot", "main", "develop"]
    )
    suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
    return f"{prefix}/{suffix}-{_counter}"


class TestRunIndexProperties:
    """Property: two distinct run IDs never produce overlapping paths."""

    @staticmethod
    def _all_paths(idx):
        """Return every filesystem path the RunIndex touches."""
        return {idx.dir, idx.json, idx.state_json}

    def test_distinct_run_ids_never_overlap(self, tmp_path):
        seen: set[Path] = set()
        for _ in range(100):
            idx = RunIndex.create(tmp_path, _random_run_id())
            paths = self._all_paths(idx)
            # Each run's paths must not overlap with any other run's paths.
            overlap = seen & paths
            assert not overlap, (
                f"run_id={idx.run_id!r} produced paths that overlap with a previous run: {overlap}"
            )
            seen |= paths

    def test_same_run_id_same_paths(self, tmp_path):
        idx_a = RunIndex.create(tmp_path, "feature/auth")
        idx_b = RunIndex.create(tmp_path, "feature/auth")
        assert idx_a.dir == idx_b.dir
        assert idx_a.json == idx_b.json

    def test_slugified_run_id_only_uses_permitted_characters(self):
        for _ in range(200):
            raw = _random_run_id()
            safe = slugify(raw)
            assert "/" not in safe, f"slugify({raw!r}) -> {safe!r} still contains '/'"
            assert "\\" not in safe, f"slugify({raw!r}) -> {safe!r} still contains '\\'"
            assert " " not in safe, f"slugify({raw!r}) -> {safe!r} still contains ' '"
            # Every character must be alphanumeric or hyphen.
            for ch in safe:
                assert (
                    ch.isalnum() or ch == "-"
                ), f"slugify({raw!r}) -> {safe!r} contains {ch!r}"
            # Slugified result is never empty unless input is empty.
            if raw.strip():
                assert safe, f"slugify({raw!r}) -> empty string"
