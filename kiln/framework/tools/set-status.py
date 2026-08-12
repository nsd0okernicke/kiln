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

import json
import os
import sys
from datetime import UTC, datetime
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

USAGE = (
    "Usage: set-status.py <role> <state> [detail] "
    "[--mode=auto|manual] [--cycles=N] [--cost=X.XX] "
    "[--tokens-in=N] [--tokens-out=N] [--tokens-cache-read=N] [--tokens-cache-write=N]"
)

#: Flag -> key in the status file's `token_usage` object. Each kind arrives as its own
#: scalar flag because this script is copied verbatim into every worktree and cannot import
#: scheduler.adapters.TokenUsage to unpack a structured value.
#:
#: The breakdown is kept rather than pre-summed because it is the actionable part: a large
#: `cache_read` is cheap and healthy, a large `input` is prompt bloat, and one total cannot
#: tell those apart.
TOKEN_FLAGS = {
    "--tokens-in=": "input",
    "--tokens-out=": "output",
    "--tokens-cache-read=": "cache_read",
    "--tokens-cache-write=": "cache_write",
}


def parse_argv(argv):
    """
    Parse `role, state, detail, mode, cycles, cost_usd, tokens` from argv (excluding the
    script name).

    `tokens` is a dict of the `TOKEN_FLAGS` kinds that were actually passed, or None when
    none were. `cycles`/`cost_usd`/`tokens` are None when not passed -- only
    role_scheduler.py's scheduler-driven roles track these; wrapper-mode roles never pass
    them, and build_status() omits them from the written JSON entirely in that case rather
    than writing a misleading 0.

    Raises ValueError (message is the usage string) if `role`/`state` are missing.
    """
    if len(argv) < 2:
        raise ValueError(USAGE)

    role = argv[0]
    state = argv[1]
    detail = argv[2] if len(argv) > 2 and argv[2] != "-" and not argv[2].startswith("--") else None

    mode = "auto"
    cycles = None
    cost_usd = None
    tokens = {}
    for arg in argv[2:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg.startswith("--cycles="):
            cycles = int(arg.split("=", 1)[1])
        elif arg.startswith("--cost="):
            cost_usd = float(arg.split("=", 1)[1])
        else:
            for flag, key in TOKEN_FLAGS.items():
                if arg.startswith(flag):
                    tokens[key] = int(arg.split("=", 1)[1])
                    break

    return role, state, detail, mode, cycles, cost_usd, tokens or None


def build_status(
    role: str,
    state: str,
    detail: str | None,
    mode: str,
    cycles: int | None = None,
    cost_usd: float | None = None,
    tokens: dict | None = None,
) -> dict:
    """Build the status dict for one role. Raises ValueError for an unrecognized state."""
    if state not in STATE_EMOJIS:
        raise ValueError(f"unknown state '{state}'")

    # The display title is built once, shared by the JSON file and the OSC sequence, so
    # renderers (e.g. the WezTerm status bar) don't need their own copy of STATE_EMOJIS.
    emoji = STATE_EMOJIS[state]
    title = f"{role} {emoji} {state}"
    if detail:
        title += f": {detail}"

    status = {
        "role": role,
        "state": state,
        "detail": detail,
        "mode": mode,
        "since": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "title": title,
    }
    # Omitted, not written as 0/None, when absent: a wrapper-mode role's status file should
    # not claim "$0.00 spent, 0 cycles" when it never tracked either in the first place.
    if cycles is not None:
        status["cycles"] = cycles
    if cost_usd is not None:
        status["cost_usd"] = cost_usd
    if tokens:
        # Both: `tokens` is the one number the pane bar and the dashboard's TOKENS column
        # want, `token_usage` is the breakdown the optimization work needs. Deriving the
        # total here rather than accepting it as a flag keeps them from disagreeing.
        status["tokens"] = sum(tokens.values())
        status["token_usage"] = tokens
    return status


def project_root_from_own_path():
    """
    Derive the project root from where this script sits, as a fallback.

    `KILN_PROJECT_DIR` is exported only by the WezTerm backend, so under tmux or Windows
    Terminal every call died with "environment variable not set" and `.kiln/status/` stayed
    empty — taking the dashboard's whole STATE column, the persisted cycle/cost totals, and
    the "just cat .kiln/status/<role>.json" fallback the README offers non-WezTerm users
    down with it. Found on Linux, where tmux is the only fallback there is.

    `copy_framework_tools()` always installs this file at `<project>/.kiln/tools/`, so its
    own location identifies the project without any cooperation from the launcher. `resolve()`
    matters: in a worktree `.kiln` is a symlink to the real project's, and following it is
    what makes every role write into the one shared status directory.
    """
    tools_dir = Path(__file__).resolve().parent
    if tools_dir.name == "tools" and tools_dir.parent.name == ".kiln":
        return str(tools_dir.parent.parent)
    return None


def main():
    try:
        role, state, detail, mode, cycles, cost_usd, tokens = parse_argv(sys.argv[1:])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    try:
        status = build_status(role, state, detail, mode, cycles, cost_usd, tokens)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    project_dir = os.environ.get("KILN_PROJECT_DIR") or project_root_from_own_path()
    if not project_dir:
        print("Error: KILN_PROJECT_DIR environment variable not set", file=sys.stderr)
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
