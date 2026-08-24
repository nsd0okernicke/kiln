#!/usr/bin/env python3
"""
Claude Code PreToolUse/PostToolUse hook that writes agent status deterministically
from tool-call events, instead of relying on the loop templates' inline
`set-status.py` bash calls (which the agent can skip or reorder under long runs).

Wired via each worktree's .claude/settings.json "hooks" block. Reads the hook
event JSON on stdin, infers the calling role from the worktree's own .mcp.json
(KILN_ROLE), and shells out to set-status.py for the actual write.

Must never raise or block the tool call it's attached to — any failure here
is swallowed so the underlying tool call always proceeds normally.
"""

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path


def detect_role(cwd: str):
    mcp_path = Path(cwd) / ".mcp.json"
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        return data["mcpServers"]["kiln-channel"]["env"]["KILN_ROLE"]
    except Exception:
        return None


def infer_status(payload):
    """Return the state/detail transition represented by one Claude hook payload."""
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if event == "PreToolUse":
        return _pre_tool_status(tool_name, tool_input)
    if event == "PostToolUse":
        return _post_tool_status(tool_name, payload.get("tool_response"))
    return None, None


def _post_tool_status(tool_name, response):
    response = response or {}
    if tool_name == "mcp__kiln-channel__wait_for_message" and response.get("received"):
        return "receiving", None
    return None, None


def _pre_tool_status(tool_name, tool_input):
    if tool_name == "mcp__kiln-channel__wait_for_message":
        return "waiting", None
    subagent = str(tool_input.get("subagent_type", ""))
    if tool_name in ("Task", "Agent") and subagent.endswith("-worker"):
        return "delegating", tool_input.get("subagent_type")
    if tool_name == "Skill" and tool_input.get("skill") == "kiln-handoff":
        return "handoff", None
    return None, None


def main():
    payload = json.load(sys.stdin)
    cwd = payload.get("cwd") or os.getcwd()
    state, detail = infer_status(payload)

    if not state:
        return

    command = _status_command(cwd, state, detail)
    if command is None:
        return

    # sys.executable, not a bare "python": this file is copied into each worktree and run by
    # the agent CLI's hook runner, and stock Debian/Ubuntu has no `python` on PATH at all —
    # only `python3`. The interpreter already running this hook is by definition a working one.
    subprocess.run(command, cwd=cwd, timeout=10, capture_output=True)


def _status_command(cwd, state, detail):
    role = detect_role(cwd)
    script = Path(cwd) / ".kiln" / "tools" / "set-status.py"
    if not role or not script.exists():
        return None
    args = [sys.executable, str(script), role, state]
    if detail:
        args.append(detail)
    args.append("--mode=auto")
    return args


if __name__ == "__main__":
    # Never let a status-reporting failure surface to the tool call this hook is attached to.
    with contextlib.suppress(Exception):
        main()
