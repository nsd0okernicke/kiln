"""Unit tests for RunIndex, slugify, and path derivation."""

from __future__ import annotations

import json
from pathlib import Path

from kiln.launcher.domain.paths import RunIndex, slugify

# ── slugify ────────────────────────────────────────────────────────────────────


class TestSlugify:
    def test_branch_name_with_slash_becomes_hyphen(self):
        assert slugify("feature/auth") == "feature-auth"

    def test_branch_name_stays_unchanged_when_already_clean(self):
        assert slugify("main") == "main"

    def test_case_is_lowered(self):
        assert slugify("Feature/Auth") == slugify("feature/auth")

    def test_special_characters_replaced(self):
        assert slugify("bug #42") == "bug--42"

    def test_backslash_is_replaced(self):
        assert slugify("feature\\auth") == "feature-auth"

    def test_dot_is_replaced(self):
        assert slugify("v1.0") == "v1-0"

    def test_spaces_become_hyphens(self):
        assert slugify("my branch") == "my-branch"

    def test_leading_and_trailing_whitespace_stripped(self):
        assert slugify("  branch  ") == "--branch--"

    def test_empty_string_returns_empty_string(self):
        assert slugify("") == ""


# ── RunIndex ───────────────────────────────────────────────────────────────────


class TestRunIndex:
    def test_create_derives_paths_from_original_root(self, tmp_path):
        idx = RunIndex.create(tmp_path, "feature/auth")
        assert idx.run_id == "feature-auth"
        assert idx.original_root == tmp_path.resolve()
        assert idx.dir.as_posix().endswith(".kiln/runs/feature-auth")
        assert idx.json.as_posix().endswith(".kiln/runs/feature-auth/run.json")
        assert idx.state_json.as_posix().endswith(".kiln/runs/feature-auth/state.json")

    def test_from_run_dir_reconstructs_from_index_path(self, tmp_path):
        run_dir = tmp_path / ".kiln" / "runs" / "exp-auth"
        run_dir.mkdir(parents=True)
        idx = RunIndex.from_run_dir(run_dir)
        assert idx.run_id == "exp-auth"
        assert idx.original_root == tmp_path.resolve()

    def test_run_json_is_writable_and_readable(self, tmp_path):
        idx = RunIndex.create(tmp_path, "test-run")
        idx.dir.mkdir(parents=True, exist_ok=True)
        data = {"run_id": "test-run", "checkout_path": str(tmp_path / ".worktrees" / "test-run")}
        idx.json.write_text(json.dumps(data) + "\n", encoding="utf-8")

        loaded = json.loads(idx.json.read_text(encoding="utf-8"))
        assert loaded["run_id"] == "test-run"

    def test_two_runs_have_distinct_index_directories(self, tmp_path):
        idx_a = RunIndex.create(tmp_path, "run-a")
        idx_b = RunIndex.create(tmp_path, "run-b")
        assert idx_a.dir != idx_b.dir
        assert idx_a.json != idx_b.json

    def test_run_dir_is_subdirectory_of_kiln_runs(self, tmp_path):
        idx = RunIndex.create(tmp_path, "my-run")
        assert idx.dir.parent.name == "runs"
        assert idx.dir.parent.parent.name == ".kiln"


# ── KilnPaths unchanged ────────────────────────────────────────────────────────


class TestKilnPathsInterface:
    """Smoke test that KilnPaths still exposes all its original properties."""

    def test_original_properties_work(self, tmp_path):
        from kiln.launcher.domain.paths import KilnPaths

        p = KilnPaths.create(tmp_path, tmp_path / "framework")
        # Ephemeral state paths
        assert p.state_dir == tmp_path / ".kiln"
        assert p.db_path == tmp_path / ".kiln" / "messages.db"
        assert p.worktrees_dir == tmp_path / ".worktrees"
        # Per-role paths
        assert p.worktree_path("coder") == tmp_path / ".worktrees" / "coder"
        assert p.codex_home("coder") == tmp_path / ".kiln" / "codex-home" / "coder"
