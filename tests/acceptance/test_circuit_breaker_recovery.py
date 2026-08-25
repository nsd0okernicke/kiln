"""Acceptance scenario: a halted scheduler resumes only after an operator retry."""

import json
import subprocess
import time

from conftest import REPO_ROOT, console_script
from workflow_support import fake_environment, prepare, rows, scheduler_command, send


def _wait_until(predicate, process: subprocess.Popen, stderr_path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(stderr_path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError(
        f"condition not reached; stderr:\n{stderr_path.read_text(encoding='utf-8')}"
    )


def test_halted_scheduler_resumes_after_public_retry(
    initialized_project, command_runner, fake_claude
):
    prepare(initialized_project, command_runner)
    inbound_id = send(command_runner, initialized_project, "trip then reset breaker")
    sequence = initialized_project / ".kiln" / "fake-sequence.txt"
    sequence.write_text("blocked\ndone\n", encoding="utf-8")
    stdout_path = command_runner.report_dir / "circuit-stdout.log"
    stderr_path = command_runner.report_dir / "circuit-stderr.log"
    command = scheduler_command(initialized_project, escalation_limit=1, max_attempts=1, once=False)
    environment = {
        **command_runner.environment,
        **fake_environment(
            command_runner,
            fake_claude,
            status="blocked",
            sequence_file=sequence,
        ),
    }
    (command_runner.report_dir / "circuit-command.json").write_text(
        json.dumps({"command": [str(part) for part in command]}, indent=2), encoding="utf-8"
    )
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=REPO_ROOT,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            text=True,
        )
    try:
        _wait_until(
            lambda: any(
                row["id"].startswith(inbound_id) and row["status"] == "failed"
                for row in rows(initialized_project)
            ),
            process,
            stderr_path,
        )
        time.sleep(0.2)
        assert sequence.read_text(encoding="utf-8").splitlines() == ["done"]

        retry = command_runner.run(
            console_script("kiln"),
            "retry",
            inbound_id,
            "--guidance",
            "operator reset after inspection",
            "--working-dir",
            initialized_project,
            cwd=REPO_ROOT,
        )
        assert "resumed" in retry.stdout
        _wait_until(
            lambda: any(
                row["id"].startswith(inbound_id) and row["status"] == "processed"
                for row in rows(initialized_project)
            ),
            process,
            stderr_path,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    log = stderr_path.read_text(encoding="utf-8")
    assert "CIRCUIT BREAKER" in log
    assert "resumed by a human" in log
    assert sequence.read_text(encoding="utf-8") == ""
