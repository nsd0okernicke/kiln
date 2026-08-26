"""Shared operations for process-level acceptance scenarios."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from conftest import REPO_ROOT, CommandRunner, console_script


def git(runner: CommandRunner, project: Path, *args: str):
    return runner.run("git", *args, cwd=project)


def prepare(
    project: Path,
    runner: CommandRunner,
    *,
    profile: str = "spike",
    agent_override: str | None = "claude",
) -> None:
    (project / "verify_system.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "expected = Path(os.environ.get('KILN_VERIFY_FILE', 'system-worker.txt'))\n"
        "passed = os.environ.get('KILN_VERIFY_STATUS', 'pass') == 'pass'\n"
        "raise SystemExit(0 if passed and expected.read_text(encoding='utf-8') else 1)\n",
        encoding="utf-8",
    )
    git(runner, project, "add", "-A")
    git(runner, project, "commit", "-m", "Initial system-test project")
    command = [
        console_script("kiln"),
        "--working-dir",
        project,
        "--profile",
        profile,
        "--terminal",
        "none",
        "--dry-run",
    ]
    if agent_override:
        command += ["--agent-override", agent_override]
    runner.run(*command, cwd=REPO_ROOT)


def send(
    runner: CommandRunner,
    project: Path,
    summary: str,
    *,
    target: str = "coder",
    handoff: str = "system-test-task",
    commit: str = "",
) -> str:
    command = [
        console_script("kiln"),
        "send",
        summary,
        "--to",
        target,
        "--handoff",
        handoff,
        "--working-dir",
        project,
    ]
    if commit:
        command += ["--commit", commit]
    result = runner.run(*command, cwd=REPO_ROOT)
    return result.stdout.partition("id=")[2].partition(")")[0]


def scheduler_command(
    project: Path,
    *,
    role: str = "coder",
    target: str = "human-in-the-loop",
    max_attempts: int = 1,
    escalation_limit: int = 3,
    once: bool = True,
    agent: str = "claude",
) -> list[str | Path]:
    command: list[str | Path] = [
        console_script("kiln-scheduler"),
        "--role",
        role,
        "--branch",
        "main",
        "--db-path",
        project / ".kiln" / "messages.db",
        "--worktree",
        project / ".worktrees" / role,
        "--workflow",
        project / "kiln" / "project" / "constitution" / "workflow.md",
        "--worker-agent",
        project / f".{agent}" / "agents" / f"{role}-worker.md",
        "--agent",
        agent,
        "--route",
        f"{role}={target}",
        "--max-attempts",
        str(max_attempts),
        "--escalation-limit",
        str(escalation_limit),
        "--worker-timeout",
        "10",
        "--worker-idle-timeout",
        "0",
        "--verify",
        f'"{sys.executable}" verify_system.py',
        "--verify-timeout",
        "10",
        "--no-status-bar",
    ]
    if once:
        command.append("--once")
    return command


def fake_environment(
    runner: CommandRunner,
    fake_claude: Path,
    *,
    status: str,
    fake_file: str = "system-worker.txt",
    verification_status: str = "pass",
    sequence_file: Path | None = None,
    executable: str = "claude",
) -> dict[str, str]:
    path = str(fake_claude) + os.pathsep + runner.environment.get("PATH", "")
    resolved = shutil.which(executable, path=path)
    expected = fake_claude / (f"{executable}.exe" if os.name == "nt" else executable)
    assert resolved and Path(resolved).resolve() == expected.resolve(), (
        f"refusing to run scheduler: fake Claude did not win PATH resolution ({resolved})"
    )
    environment = {
        "PATH": path,
        "PYTHONPATH": str(fake_claude) + os.pathsep + runner.environment.get("PYTHONPATH", ""),
        "KILN_FAKE_STATUS": status,
        "KILN_FAKE_HANDOFF": "system-test-task",
        "KILN_FAKE_FILE": fake_file,
        "KILN_VERIFY_FILE": fake_file,
        "KILN_VERIFY_STATUS": verification_status,
    }
    if sequence_file is not None:
        environment["KILN_FAKE_SEQUENCE_FILE"] = str(sequence_file)
    return environment


def scheduler(
    runner: CommandRunner,
    project: Path,
    fake_claude: Path,
    *,
    status: str,
    max_attempts: int = 1,
    role: str = "coder",
    target: str = "human-in-the-loop",
    fake_file: str = "system-worker.txt",
    verification_status: str = "pass",
    agent: str = "claude",
):
    return runner.run(
        *scheduler_command(
            project,
            role=role,
            target=target,
            max_attempts=max_attempts,
            once=True,
            agent=agent,
        ),
        cwd=REPO_ROOT,
        env=fake_environment(
            runner,
            fake_claude,
            status=status,
            fake_file=fake_file,
            verification_status=verification_status,
            executable=agent,
        ),
    )


def rows(project: Path) -> list[dict]:
    with closing(sqlite3.connect(project / ".kiln" / "messages.db")) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY rowid")]


def request(url: str, path: str, *, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-Kiln-Cockpit": "1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    http_request = urllib.request.Request(url + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(http_request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
