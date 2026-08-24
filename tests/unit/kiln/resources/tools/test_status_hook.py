import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def status_hook():
    path = Path(__file__).parents[5] / "src" / "kiln" / "resources" / "tools" / "status-hook.py"
    spec = importlib.util.spec_from_file_location("kiln_status_hook", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__kiln-channel__wait_for_message",
            },
            ("waiting", None),
        ),
        (
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Task",
                "tool_input": {"subagent_type": "coder-worker"},
            },
            ("delegating", "coder-worker"),
        ),
        (
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Skill",
                "tool_input": {"skill": "kiln-handoff"},
            },
            ("handoff", None),
        ),
        (
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__kiln-channel__wait_for_message",
                "tool_response": {"received": True},
            },
            ("receiving", None),
        ),
        ({"hook_event_name": "PostToolUse", "tool_name": "Task"}, (None, None)),
        ({"hook_event_name": "SomethingElse"}, (None, None)),
        (
            {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {}},
            (None, None),
        ),
    ],
)
def test_infer_status(status_hook, payload, expected):
    assert status_hook.infer_status(payload) == expected


def test_detect_role_reads_worktree_configuration(status_hook, tmp_path):
    config = {
        "mcpServers": {"kiln-channel": {"env": {"KILN_ROLE": "coder"}}},
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")

    assert status_hook.detect_role(str(tmp_path)) == "coder"


def test_detect_role_tolerates_invalid_configuration(status_hook, tmp_path):
    (tmp_path / ".mcp.json").write_text("not json", encoding="utf-8")
    assert status_hook.detect_role(str(tmp_path)) is None


def test_main_invokes_set_status_for_inferred_transition(status_hook, tmp_path, monkeypatch):
    script = tmp_path / ".kiln" / "tools" / "set-status.py"
    script.parent.mkdir(parents=True)
    script.touch()
    payload = {
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "reviewer-worker"},
    }
    calls = []
    monkeypatch.setattr(status_hook.json, "load", lambda stream: payload)
    monkeypatch.setattr(status_hook, "detect_role", lambda cwd: "lead")
    monkeypatch.setattr(
        status_hook.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    status_hook.main()

    assert calls == [
        (
            (
                [
                    sys.executable,
                    str(script),
                    "lead",
                    "delegating",
                    "reviewer-worker",
                    "--mode=auto",
                ],
            ),
            {"cwd": str(tmp_path), "timeout": 10, "capture_output": True},
        )
    ]


def test_main_ignores_unmapped_events(status_hook, monkeypatch):
    monkeypatch.setattr(
        status_hook.json,
        "load",
        lambda stream: {"hook_event_name": "PostToolUse", "tool_name": "Task"},
    )
    monkeypatch.setattr(
        status_hook,
        "detect_role",
        lambda cwd: pytest.fail("role detection should not be reached"),
    )

    status_hook.main()


def test_main_ignores_a_missing_role(status_hook, tmp_path, monkeypatch):
    payload = {
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__kiln-channel__wait_for_message",
    }
    monkeypatch.setattr(status_hook.json, "load", lambda stream: payload)
    monkeypatch.setattr(status_hook, "detect_role", lambda cwd: None)
    monkeypatch.setattr(
        status_hook.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("set-status should not run"),
    )
    status_hook.main()
