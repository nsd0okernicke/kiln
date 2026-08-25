"""Shared process-level fixtures for Kiln acceptance scenarios."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = REPO_ROOT / "reports" / "acceptance"


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(str(item.path)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.acceptance)


@dataclass(frozen=True)
class CompletedCommand:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(self, report_dir: Path, environment: dict[str, str]):
        self.report_dir = report_dir
        self.environment = environment
        self.calls = 0

    def run(
        self,
        *command: str | Path,
        cwd: Path,
        env: dict[str, str] | None = None,
        check: bool = True,
        timeout: float = 45,
    ) -> CompletedCommand:
        self.calls += 1
        argv = tuple(str(part) for part in command)
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env={**self.environment, **(env or {})},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        result = CompletedCommand(argv, completed.returncode, completed.stdout, completed.stderr)
        self._record(result)
        if check and completed.returncode:
            pytest.fail(
                f"command failed ({completed.returncode}): {' '.join(argv)}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return result

    def _record(self, result: CompletedCommand) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.calls:02d}"
        (self.report_dir / f"{stem}-command.json").write_text(
            json.dumps(
                {"command": result.command, "returncode": result.returncode},
                indent=2,
            ),
            encoding="utf-8",
        )
        (self.report_dir / f"{stem}-stdout.log").write_text(result.stdout, encoding="utf-8")
        (self.report_dir / f"{stem}-stderr.log").write_text(result.stderr, encoding="utf-8")


def console_script(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = Path(sys.executable).parent / f"{name}{suffix}"
    if not path.is_file():
        pytest.fail(f"console script is not installed beside the test interpreter: {path}")
    return path


@pytest.fixture
def command_runner(request, tmp_path, monkeypatch):
    report_dir = REPORT_ROOT / request.node.name
    if report_dir.exists():
        shutil.rmtree(report_dir)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "GIT_AUTHOR_NAME": "Kiln System Test",
            "GIT_AUTHOR_EMAIL": "system@kiln.invalid",
            "GIT_COMMITTER_NAME": "Kiln System Test",
            "GIT_COMMITTER_EMAIL": "system@kiln.invalid",
        }
    )
    return CommandRunner(report_dir, environment)


@pytest.fixture
def fake_claude(tmp_path) -> Path:
    binary_dir = tmp_path / "fake-bin"
    binary_dir.mkdir()
    worker = Path(__file__).parent / "fixtures" / "fake_claude.py"
    python = sys.executable

    if os.name == "nt":
        from pip._vendor.distlib.scripts import ScriptMaker

        shutil.copyfile(worker, binary_dir / "kiln_system_fake_claude.py")
        maker = ScriptMaker(None, str(binary_dir))
        maker.clobber = True
        maker.variants = {""}
        maker.make("claude = kiln_system_fake_claude:main")

    posix = binary_dir / "claude"
    posix.write_text(f'#!/bin/sh\nexec "{python}" "{worker}" "$@"\n', encoding="utf-8")
    posix.chmod(0o755)
    # Kept as a convenience for an interactive diagnosis. Scheduler tests resolve the real
    # generated .exe above on Windows, because CreateProcess does not reliably choose .cmd.
    (binary_dir / "claude.cmd").write_text(
        f'@echo off\r\n"{python}" "{worker}"\r\n', encoding="utf-8"
    )
    return binary_dir


@pytest.fixture
def initialized_project(tmp_path, command_runner):
    project = tmp_path / "library-hub"
    command_runner.run(
        console_script("kiln"),
        "init",
        project,
        "--example",
        "library-hub",
        cwd=REPO_ROOT,
    )
    return project
