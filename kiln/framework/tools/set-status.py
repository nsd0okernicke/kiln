#!/usr/bin/env python3
"""
Write agent status to both a JSON file and terminal title (OSC 0 escape sequence).
Used by wrapper agents in loop templates to signal state transitions visibly.

Usage: python set-status.py <role> <state> [detail]
  role: agent role name (e.g., "coder", "architect")
  state: one of "waiting", "receiving", "delegating", "handoff"
  detail: optional detail string (e.g., role name of delegated worker, or "-" to clear)
"""

import sys
import json
from datetime import datetime, timezone
from pathlib import Path

STATE_EMOJIS = {
    "waiting": "⏳",
    "receiving": "🔀",
    "delegating": "⚙",
    "handoff": "↩",
}

def main():
    if len(sys.argv) < 3:
        print("Usage: set-status.py <role> <state> [detail]", file=sys.stderr)
        sys.exit(1)

    role = sys.argv[1]
    state = sys.argv[2]
    detail = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None

    if state not in STATE_EMOJIS:
        print(f"Error: unknown state '{state}'", file=sys.stderr)
        sys.exit(1)

    # Determine status directory
    kiln_dir = Path(__file__).parent.parent
    status_dir = kiln_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)

    # Build the display title once, shared by the JSON file and the OSC
    # sequence, so renderers (e.g. the WezTerm status bar) don't need their
    # own copy of STATE_EMOJIS.
    emoji = STATE_EMOJIS[state]
    title = f"{role} {emoji} {state}"
    if detail:
        title += f": {detail}"

    # Write status JSON
    status_file = status_dir / f"{role}.json"
    status = {
        "role": role,
        "state": state,
        "detail": detail,
        "since": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "title": title,
    }
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
    osc_sequence = f"\033]0;{title}\007"
    sys.stdout.buffer.write(osc_sequence.encode("utf-8"))
    sys.stdout.buffer.flush()

if __name__ == "__main__":
    main()
