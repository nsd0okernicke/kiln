#!/usr/bin/env python3
"""
Write agent status to both a JSON file and terminal title (OSC 0 escape sequence).
Used by wrapper agents in loop templates to signal state transitions visibly.

Usage: python set-status.py <role> <state> [detail]
  role: agent role name (e.g., "coder", "architect")
  state: one of STATE_EMOJIS's keys below
  detail: optional detail string (e.g., role name of delegated worker, or "-" to clear)

`STATE_EMOJIS`'s keys must match `scheduler.pane_status.STATE_COLORS_HEX`'s exactly (see
`tests/test_set_status.py`'s parity test) — this script is copied verbatim into every
worktree by `workspace.copy_framework_tools()` and can't import that module at runtime (it
may not be on `sys.path` there, and this file's hyphenated name means it can't be imported
either), so the two dicts are kept in sync by hand, guarded by that test rather than code.
"""

import sys
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_EMOJIS = {
    "starting": "🌱",
    "waiting": "💤",
    "idle": "💤",
    "receiving": "📥",
    "working": "🚀",
    "delegating": "🔀",
    "approval": "👁️",
    "retrying": "🔁",
    "handoff": "📤",
    "handing-off": "📤",
    "blocked": "🚧",
    "escalated": "🆘",
    "halted": "🛑",
}

USAGE = "Usage: set-status.py <role> <state> [detail] [--mode=auto|manual]"


def parse_argv(argv):
    """
    Parse `role, state, detail, mode` from argv (excluding the script name).

    Raises ValueError (message is the usage string) if `role`/`state` are missing.
    """
    if len(argv) < 2:
        raise ValueError(USAGE)

    role = argv[0]
    state = argv[1]
    detail = argv[2] if len(argv) > 2 and argv[2] != "-" and not argv[2].startswith("--") else None

    mode = "auto"
    for arg in argv[2:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            break

    return role, state, detail, mode


def build_status(role: str, state: str, detail: str | None, mode: str) -> dict:
    """Build the status dict for one role. Raises ValueError for an unrecognized state."""
    if state not in STATE_EMOJIS:
        raise ValueError(f"unknown state '{state}'")

    # The display title is built once, shared by the JSON file and the OSC sequence, so
    # renderers (e.g. the WezTerm status bar) don't need their own copy of STATE_EMOJIS.
    emoji = STATE_EMOJIS[state]
    title = f"{role} {emoji} {state}"
    if detail:
        title += f": {detail}"

    return {
        "role": role,
        "state": state,
        "detail": detail,
        "mode": mode,
        "since": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "title": title,
    }


def main():
    try:
        role, state, detail, mode = parse_argv(sys.argv[1:])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    try:
        status = build_status(role, state, detail, mode)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Determine status directory from project root environment variable
    project_dir = os.environ.get("Kiln_PROJECT_DIR")
    if not project_dir:
        print("Error: Kiln_PROJECT_DIR environment variable not set", file=sys.stderr)
        sys.exit(1)

    status_dir = Path(project_dir) / ".kiln" / "status"
    status_dir.mkdir(parents=True, exist_ok=True)

    status_file = status_dir / f"{role}.json"
    status_file.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    # Emit OSC 0 title-set escape sequence (unreliable as a display channel —
    # the Claude Code process sharing this pane also writes its own OSC 0
    # title updates on its render loop and will usually win the race; the
    # JSON file above is the reliable channel for anything polling status).
    # Written as raw UTF-8 bytes via the buffer, not sys.stdout.write(str) —
    # on Windows, stdout's text-mode encoding defaults to the console
    # codepage (often cp1252), which can't represent the emoji and raises
    # UnicodeEncodeError, crashing after the JSON write above already
    # succeeded. Bypassing the text encoder avoids that regardless of
    # codepage.
    osc_sequence = f"\033]0;{status['title']}\007"
    sys.stdout.buffer.write(osc_sequence.encode("utf-8"))
    sys.stdout.buffer.flush()

if __name__ == "__main__":
    main()
