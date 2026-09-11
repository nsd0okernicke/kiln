"""Unit tests for run registry discovery."""

from __future__ import annotations

import json
from pathlib import Path

from kiln.cockpit.infrastructure.registry import RunConfig, _find_run, discover_runs


def test_discover_runs_empty_when_no_runs_dir(tmp_path):
    assert discover_runs(tmp_path) == []


def test_discover_runs_empty_when_no_run_json(tmp_path):
    runs_dir = tmp_path / ".kiln" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "my-run").mkdir()
    assert discover_runs(tmp_path) == []


def test_discover_runs_reads_valid_run_json(tmp_path):
    _create_run_json(tmp_path, "exp-auth", {"run_id": "exp-auth", "checkout_path": str(tmp_path / ".worktrees" / "exp-auth")})
    (tmp_path / ".worktrees" / "exp-auth").mkdir(parents=True)

    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].run_id == "exp-auth"
    assert runs[0].checkout_path == (tmp_path / ".worktrees" / "exp-auth").resolve()


def test_discover_runs_skips_corrupt_run_json(tmp_path):
    runs_dir = tmp_path / ".kiln" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "bad").mkdir()
    (runs_dir / "bad" / "run.json").write_text("not json", encoding="utf-8")

    assert discover_runs(tmp_path) == []


def test_discover_runs_skips_missing_checkout(tmp_path):
    _create_run_json(tmp_path, "ghost", {"run_id": "ghost", "checkout_path": "/nonexistent"})

    runs = discover_runs(tmp_path)
    assert len(runs) == 0


def test_discover_runs_returns_multiple_runs(tmp_path):
    for run_id in ("alpha", "beta"):
        _create_run_json(tmp_path, run_id, {"run_id": run_id, "checkout_path": str(tmp_path / ".worktrees" / run_id)})
        (tmp_path / ".worktrees" / run_id).mkdir(parents=True)

    runs = discover_runs(tmp_path)
    assert len(runs) == 2
    assert {r.run_id for r in runs} == {"alpha", "beta"}


def test_find_run_returns_none_for_unknown(tmp_path):
    assert _find_run(tmp_path, "nonexistent") is None


def test_find_run_returns_config_for_known(tmp_path):
    _create_run_json(tmp_path, "my-run", {"run_id": "my-run", "checkout_path": str(tmp_path / ".worktrees" / "my-run")})
    (tmp_path / ".worktrees" / "my-run").mkdir(parents=True)

    run = _find_run(tmp_path, "my-run")
    assert run is not None
    assert run.run_id == "my-run"


def test_run_config_derives_paths(tmp_path):
    checkout = tmp_path / ".worktrees" / "test-run"
    checkout.mkdir(parents=True)
    rc = RunConfig(run_id="test-run", project_root=tmp_path, checkout_path=checkout)

    assert rc.db_path == checkout / ".kiln" / "messages.db"
    assert rc.status_dir == checkout / ".kiln" / "status"
    assert rc.sessions_file == checkout / ".kiln" / "sessions"
    assert rc.logs_dir == checkout / ".kiln" / "logs"
    assert rc.traffic_db == checkout / ".kiln" / "traffic.db"


def test_run_config_is_alive_when_db_exists(tmp_path):
    checkout = tmp_path / ".worktrees" / "alive"
    (checkout / ".kiln").mkdir(parents=True)
    (checkout / ".kiln" / "messages.db").write_text("", encoding="utf-8")
    rc = RunConfig(run_id="alive", project_root=tmp_path, checkout_path=checkout)

    assert rc.is_alive() is True


def test_run_config_is_not_alive_without_db(tmp_path):
    checkout = tmp_path / ".worktrees" / "dead"
    checkout.mkdir(parents=True)
    rc = RunConfig(run_id="dead", project_root=tmp_path, checkout_path=checkout)

    assert rc.is_alive() is False


def _create_run_json(base: Path, run_id: str, data: dict) -> Path:
    """Helper: write a run.json for testing."""
    run_dir = base / ".kiln" / "runs" / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "run.json"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path
