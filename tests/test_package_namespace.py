"""Contracts for Kiln's single namespaced scheduler package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src"


def test_legacy_scheduler_package_has_no_source_modules():
    assert not list((ROOT / "scheduler").glob("*.py"))


def test_namespaced_layers_are_importable():
    from kiln.scheduler.application.ports import MessageQueue
    from kiln.scheduler.application.use_cases.process_next_message import run_once
    from kiln.scheduler.domain.models import MessageStatus
    from kiln.scheduler.infrastructure.cli.role_scheduler import main
    from kiln.scheduler.infrastructure.persistence import SQLiteMessageQueue

    assert all((MessageQueue, run_once, MessageStatus, main, SQLiteMessageQueue))


def test_namespaced_scheduler_entrypoint_is_executable():
    result = subprocess.run(
        [sys.executable, "-m", "kiln.scheduler.infrastructure.cli.role_scheduler", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_namespaced_status_contract_is_executable():
    result = subprocess.run(
        [sys.executable, "-m", "kiln.scheduler.domain.status_contract", "--instruction"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "KILN-STATUS" in result.stdout
