"""
Run registry — discovers swarm instances from ``.kiln/runs/`` index entries.

Each named run leaves a ``run.json`` at ``.kiln/runs/<run_id>/run.json`` in the project root.
This module reads those entries, validates them, and produces ``RunConfig`` records that
the hub cockpit uses to serve per-run state.

The registry is re-read on each poll cycle (same cadence as the dashboard's own poll), so
a newly launched run appears in the hub within seconds with no restart required.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunConfig:
    """One swarm instance, resolved from its ``run.json`` index entry."""

    run_id: str
    project_root: Path
    checkout_path: Path
    branch: str = "main"
    profile: str = ""

    @property
    def db_path(self) -> Path:
        """The run's message database inside its checkout."""
        return self.checkout_path / ".kiln" / "messages.db"

    @property
    def status_dir(self) -> Path:
        return self.checkout_path / ".kiln" / "status"

    @property
    def sessions_file(self) -> Path:
        return self.checkout_path / ".kiln" / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.checkout_path / ".kiln" / "logs"

    @property
    def traffic_db(self) -> Path:
        return self.checkout_path / ".kiln" / "traffic.db"

    def is_alive(self) -> bool:
        """A run is alive if its messages.db exists (queue storage was created)."""
        return self.db_path.is_file()


def discover_runs(project_root: Path) -> list[RunConfig]:
    """
    Scan ``.kiln/runs/`` and return a ``RunConfig`` for each valid index entry.

    A ``run.json`` that is missing, unparseable, or points to a non-existent checkout
    is logged and skipped — the hub must stay up even when a run's files are deleted
    while the hub is running.
    """
    runs_dir = project_root / ".kiln" / "runs"
    if not runs_dir.is_dir():
        return []

    configs: list[RunConfig] = []
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            config = _load_run_json(entry)
            if config is not None:
                configs.append(config)
        except Exception as exc:
            log.warning("could not load run from %s: %s", entry, exc)
    return configs


def _find_run(project_root: Path, run_id: str) -> RunConfig | None:
    """
    Find a single run by ID from the registry.

    More efficient than ``discover_runs`` + filter when only one run is needed:
    avoids reading every ``run.json`` on every poll.
    """
    run_dir = project_root / ".kiln" / "runs" / run_id
    if not run_dir.is_dir():
        return None
    return _load_run_json(run_dir)


def _load_run_json(run_dir: Path) -> RunConfig | None:
    """Load one ``run.json`` and build a ``RunConfig``, or None on failure."""
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        return None

    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("corrupt run.json at %s: %s", run_json, exc)
        return None

    run_id = data.get("run_id") or run_dir.name
    checkout_raw = data.get("checkout_path")
    if not checkout_raw:
        log.warning("run %s has no checkout_path in run.json", run_id)
        return None

    checkout_path = Path(checkout_raw)
    if not checkout_path.is_dir():
        log.warning("run %s checkout missing: %s", run_id, checkout_path)
        return None

    return RunConfig(
        run_id=run_id,
        project_root=Path(data.get("project_root", checkout_raw)),
        checkout_path=checkout_path,
        branch=data.get("branch", "main"),
        profile=data.get("profile", ""),
    )
