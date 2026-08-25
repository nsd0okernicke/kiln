"""Acceptance scenario: operate the shared queue through the Cockpit API."""

import json
import subprocess
import time

from conftest import REPO_ROOT, console_script
from workflow_support import prepare, request, rows, send


def test_cockpit_and_cli_share_the_live_queue(initialized_project, command_runner):
    prepare(initialized_project, command_runner)
    url_file = initialized_project / ".kiln" / "system-cockpit-url"
    stdout_path = command_runner.report_dir / "cockpit-stdout.log"
    stderr_path = command_runner.report_dir / "cockpit-stderr.log"
    command_runner.report_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(console_script("kiln-cockpit")),
        "--db-path",
        str(initialized_project / ".kiln" / "messages.db"),
        "--status-dir",
        str(initialized_project / ".kiln" / "status"),
        "--sessions-file",
        str(initialized_project / ".kiln" / "sessions"),
        "--project-name",
        "system-library-hub",
        "--project-root",
        str(initialized_project),
        "--lanes",
        "coder",
        "--intake-role",
        "coder",
        "--port",
        "0",
        "--url-file",
        str(url_file),
        "--no-browser",
    ]
    (command_runner.report_dir / "cockpit-command.json").write_text(
        json.dumps({"command": command}, indent=2), encoding="utf-8"
    )
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=command_runner.environment,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            text=True,
        )
    try:
        for _ in range(100):
            if url_file.is_file():
                break
            if process.poll() is not None:
                raise AssertionError(stderr_path.read_text(encoding="utf-8"))
            time.sleep(0.05)
        url = url_file.read_text(encoding="utf-8").strip()

        cli_id = send(command_runner, initialized_project, "visible from cockpit")
        status, state = request(url, "/api/state")
        assert status == 200
        assert any(
            card["message_id"].startswith(cli_id) for card in state["board"]["cards"]["coder"]
        )

        status, sent = request(
            url,
            "/api/send",
            body={
                "target": "human-in-the-loop",
                "summary": "acknowledge this",
                "work_item": "system-human-note",
            },
        )
        assert status == 200
        message_id = sent["message_id"]
        assert request(url, f"/api/messages/{message_id}")[0] == 200
        assert request(url, f"/api/ack/{message_id}", body={})[0] == 200
        assert all(
            row["id"] != message_id or row["acked_at"] is not None
            for row in rows(initialized_project)
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
