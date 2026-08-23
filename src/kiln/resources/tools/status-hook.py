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


def main():
    payload = json.load(sys.stdin)

    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    cwd = payload.get("cwd") or os.getcwd()

    state = None
    detail = None

    subagent = str(tool_input.get("subagent_type", ""))

    if event == "PreToolUse":
        if tool_name == "mcp__kiln-channel__wait_for_message":
            state = "waiting"
        elif tool_name in ("Task", "Agent") and subagent.endswith("-worker"):
            state = "delegating"
            detail = tool_input.get("subagent_type")
        elif tool_name == "Skill" and tool_input.get("skill") == "kiln-handoff":
            state = "handoff"
    elif (
        event == "PostToolUse"
        and tool_name == "mcp__kiln-channel__wait_for_message"
        and tool_response.get("received")
    ):
        state = "receiving"

    if not state:
        return

    role = detect_role(cwd)
    if not role:
        return

    script = Path(cwd) / ".kiln" / "tools" / "set-status.py"
    if not script.exists():
        return

    # sys.executable, not a bare "python": this file is copied into each worktree and run by
    # the agent CLI's hook runner, and stock Debian/Ubuntu has no `python` on PATH at all —
    # only `python3`. The interpreter already running this hook is by definition a working one.
    args = [sys.executable, str(script), role, state]
    if detail:
        args.append(detail)
    args.append("--mode=auto")

    subprocess.run(args, cwd=cwd, timeout=10, capture_output=True)


if __name__ == "__main__":
    # Never let a status-reporting failure surface to the tool call this hook is attached to.
    with contextlib.suppress(Exception):
        main()
