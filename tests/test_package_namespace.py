"""Contracts for the gradual migration to the ``kiln`` package namespace."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

BRIDGED_MODULES = (
    "application",
    "infrastructure",
    "models",
    "policies",
    "ports",
    "queue_commands",
    "queue_queries",
    "queue_storage",
)


@pytest.mark.parametrize("module_name", BRIDGED_MODULES)
def test_namespaced_scheduler_modules_preserve_legacy_module_identity(module_name: str):
    namespaced = importlib.import_module(f"kiln.scheduler.{module_name}")
    legacy = importlib.import_module(f"scheduler.{module_name}")

    assert namespaced is legacy


def test_namespaced_domain_types_preserve_legacy_class_identity():
    namespaced = importlib.import_module("kiln.scheduler.models")
    legacy = importlib.import_module("scheduler.models")

    assert namespaced.MessageStatus is legacy.MessageStatus


def test_namespaced_scheduler_module_is_executable():
    result = subprocess.run(
        [sys.executable, "-m", "kiln.scheduler.role_scheduler", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_namespaced_status_contract_module_is_executable():
    result = subprocess.run(
        [sys.executable, "-m", "kiln.scheduler.status_contract", "--instruction"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "KILN-STATUS" in result.stdout
