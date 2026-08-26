from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SKILL = REPO / "src" / "kiln" / "resources" / "project" / "skills" / "kiln-constitution-setup"


@pytest.fixture(scope="module")
def evidence_module():
    path = SKILL / "scripts" / "project_evidence.py"
    spec = importlib.util.spec_from_file_location("project_evidence", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_project_routes_to_interview(evidence_module, tmp_path):
    result = evidence_module.inventory(tmp_path)

    assert result["suggested_mode"] == "interview"
    assert result["evidence_count"] == 0


def test_existing_repository_inventory_routes_to_evidence(evidence_module, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    workflow = tmp_path / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: test\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_demo(): pass\n", encoding="utf-8")

    result = evidence_module.inventory(tmp_path)

    assert result["suggested_mode"] == "repository-or-mixed"
    assert result["categories"]["manifests"] == ["pyproject.toml"]
    assert result["categories"]["ci"] == [".github/workflows/test.yml"]
    assert result["categories"]["tests"] == ["tests/test_demo.py"]


def test_incomplete_repository_preserves_available_evidence(evidence_module, tmp_path):
    (tmp_path / "README.md").write_text("# Planned service\n", encoding="utf-8")

    result = evidence_module.inventory(tmp_path)

    assert result["suggested_mode"] == "repository-or-mixed"
    assert result["categories"]["documentation"] == ["README.md"]
    assert result["categories"]["manifests"] == []


def test_runtime_and_generated_directories_are_excluded(evidence_module, tmp_path):
    hidden = tmp_path / ".worktrees" / "coder" / "pyproject.toml"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("[project]\n", encoding="utf-8")
    cache = tmp_path / "tests" / "__pycache__" / "test_demo.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"bytecode")

    assert evidence_module.inventory(tmp_path)["evidence_count"] == 0


def test_skill_requires_review_and_limits_its_write_scope():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "Wait for explicit approval" in text
    assert "Never silently overwrite" in text
    assert "Do not change `workflow.md`, roles, profiles, routing, or other skills" in text
    assert "engineering.md" in text and "project.md" in text


def test_every_linked_reference_exists():
    for name in ("repository-mode.md", "interview-mode.md", "output-contract.md"):
        assert (SKILL / "references" / name).is_file()
