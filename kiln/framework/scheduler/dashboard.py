"""
A live, cross-role dashboard — the swarm-wide view no single pane provides.

Every other pane shows one role: its own bottom-row bar (`pane_status.StatusBar`) or, on
WezTerm, a badge in the tab bar. Understanding the whole swarm today means looking at N panes
one at a time. This pane aggregates across all of them instead: current state per role, queue
depth per role, recent handoff activity (including escalations), and swarm-wide cost/cycles.

Unlike `pane_status.StatusBar` and `inbox.py`, this does **not** preserve scrollback — there
is nothing here worth scrolling back through, it's a `top`-style live view, so a full
clear-and-redraw every poll is the correct model, not a bug the way it would be for a status
bar. Reuses `pane_status.py`'s low-level colour primitives rather than re-deriving them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db, handoff, pane_status
from .role_scheduler import configure_logging, enable_unicode_output

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 2.0
DEFAULT_ACTIVITY_LIMIT = 8

ICON_TITLE = "\N{BAR CHART}"

#: Everything after a handoff message's closing banner rule, up to the next blank line --
#: matches `handoff.format_handoff`'s layout exactly (SEPARATOR, banner, SEPARATOR, summary).
_SUMMARY_RE = re.compile(re.escape(handoff.SEPARATOR) + r".*\n" + re.escape(handoff.SEPARATOR) + r"\n(?P<summary>.*)", re.DOTALL)


@dataclass(frozen=True)
class RoleSession:
    """One row of `.kiln/sessions` — the static role inventory `workspace.write_sessions_file` writes at launch."""

    role: str
    agent: str
    display_name: str


def read_sessions(path: Path) -> list[RoleSession]:
    """Parse `.kiln/sessions` (`index\\trole\\tagent\\tdisplay_name` per line)."""
    if not path.is_file():
        return []
    sessions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        _, role, agent, display_name = parts[:4]
        sessions.append(RoleSession(role=role, agent=agent, display_name=display_name))
    return sessions


def read_status(status_dir: Path, role: str) -> dict | None:
    """Read one role's `.kiln/status/<role>.json`. None when absent or unparseable."""
    path = status_dir / f"{role}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def extract_summary(content: str, max_chars: int = 60) -> str:
    """
    The one line of prose a human would read from a handoff message, condensed to one row.

    Falls back to the first non-blank line of the raw content for anything that doesn't
    match `format_handoff`'s layout (a hand-written or malformed message) rather than
    showing nothing.
    """
    match = _SUMMARY_RE.search(content)
    text = match.group("summary") if match else content
    line = next((candidate.strip() for candidate in text.splitlines() if candidate.strip()), "")
    if len(line) > max_chars:
        line = line[: max_chars - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return line


def _parse_status_since(value: str) -> datetime:
    """`set-status.py` writes UTC ISO 8601 with a trailing `Z`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_local_timestamp(value: str) -> datetime:
    """`messages.db`'s `created_at` is naive local time (the schema's own default)."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _ago(dt: datetime, now: datetime) -> str:
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _colorize(padded_text: str, state: str) -> str:
    """Colour already-padded text -- padding first keeps ANSI codes out of column widths."""
    r, g, b = pane_status.style_for(state)
    return f"\x1b[48;2;{r};{g};{b}m\x1b[38;2;0;0;0m{padded_text}{pane_status.RESET_STYLE}"


def render_state_grid(
    sessions: list[RoleSession],
    statuses: dict[str, dict],
    queue_depth: dict[str, int],
    now_utc: datetime,
) -> list[str]:
    header = f"{'ROLE':<20} {'STATE':<16} {'SINCE':<10} {'QUEUE':>5} {'CYCLES':>7} {'COST':>8}"
    lines = [header, "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * len(header)]
    for session in sessions:
        status = statuses.get(session.role)
        state = status["state"] if status else "-"
        since = (
            _ago(_parse_status_since(status["since"]), now_utc)
            if status and status.get("since")
            else "-"
        )
        cycles = status.get("cycles") if status else None
        cost = status.get("cost_usd") if status else None
        state_cell = _colorize(f"{pane_status.STATE_GLYPH} {state}".ljust(16), state)
        cycles_display = "-" if cycles is None else str(cycles)
        cost_display = "-" if cost is None else f"${cost:.2f}"
        lines.append(
            f"{session.role:<20} {state_cell} {since:<10} "
            f"{queue_depth.get(session.role, 0):>5} {cycles_display:>7} {cost_display:>8}"
        )
    return lines


def render_totals(statuses: dict[str, dict]) -> tuple[float, int]:
    total_cost = sum(status.get("cost_usd") or 0 for status in statuses.values())
    total_cycles = sum(status.get("cycles") or 0 for status in statuses.values())
    return total_cost, total_cycles


def _activity_line(row: dict, now_local: datetime) -> str:
    when = _ago(_parse_local_timestamp(row["created_at"]), now_local)
    summary = extract_summary(row["content"])
    return f"  {when:<8} {row['sender']} \N{RIGHTWARDS ARROW} {row['target']:<20} {summary}"


def render_activity(messages: list[dict], now_local: datetime, limit: int) -> list[str]:
    lines = ["", "Recent activity"]
    shown = messages[:limit]
    if not shown:
        lines.append("  (none yet)")
    lines.extend(_activity_line(row, now_local) for row in shown)
    return lines


def render_escalations(messages: list[dict], now_local: datetime) -> list[str]:
    escalations = [row for row in messages if handoff.is_escalation(row["content"])]
    lines = ["", "Escalations"]
    if not escalations:
        lines.append("  (none in the recent window)")
    lines.extend(_activity_line(row, now_local) for row in escalations)
    return lines


def render_dashboard(
    *,
    project_name: str,
    branch: str,
    sessions: list[RoleSession],
    statuses: dict[str, dict],
    queue_depth: dict[str, int],
    messages: list[dict],
    now_utc: datetime,
    now_local: datetime,
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT,
) -> list[str]:
    """Pure: given one snapshot of the world, render every section. No I/O, no clock."""
    header = f"{ICON_TITLE} Kiln Dashboard \N{EM DASH} {project_name} ({branch})"
    timestamp = now_local.strftime("%H:%M:%S")
    padding = max(2, 74 - len(header) - len(timestamp))
    title_line = f"{header}{' ' * padding}{timestamp}"
    rule = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * len(title_line)

    lines = [title_line, rule]
    lines += render_state_grid(sessions, statuses, queue_depth, now_utc)
    lines.append(rule)

    total_cost, total_cycles = render_totals(statuses)
    escalation_count = sum(1 for row in messages if handoff.is_escalation(row["content"]))
    lines.append(
        f"TOTAL COST: ${total_cost:.2f}        TOTAL CYCLES: {total_cycles}        "
        f"ESCALATIONS: {escalation_count}"
    )

    lines += render_activity(messages, now_local, activity_limit)
    lines += render_escalations(messages, now_local)
    return lines


@dataclass
class DashboardContext:
    db_path: Path
    branch: str
    status_dir: Path
    sessions_file: Path
    project_name: str
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT


def snapshot(ctx: DashboardContext) -> list[str]:
    """Gather current state from disk/DB and render one frame."""
    sessions = read_sessions(ctx.sessions_file)
    statuses = {
        session.role: status
        for session in sessions
        if (status := read_status(ctx.status_dir, session.role)) is not None
    }
    queue_depth = db.count_queued_by_role(ctx.db_path, ctx.branch)
    # A wider window than activity_limit so escalations outside the visible activity list
    # still have a chance to surface -- this is a recent-window view, not exhaustive history.
    messages = db.recent_messages(ctx.db_path, ctx.branch, limit=max(ctx.activity_limit, 20))

    return render_dashboard(
        project_name=ctx.project_name,
        branch=ctx.branch,
        sessions=sessions,
        statuses=statuses,
        queue_depth=queue_depth,
        messages=messages,
        now_utc=datetime.now(timezone.utc),
        now_local=datetime.now(),
        activity_limit=ctx.activity_limit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln dashboard", description="Live cross-role swarm dashboard."
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--status-dir", required=True)
    parser.add_argument("--sessions-file", required=True)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--activity-limit", type=int, default=DEFAULT_ACTIVITY_LIMIT)
    parser.add_argument("--once", action="store_true", help="render a single frame and exit")
    parser.add_argument("--log-file", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    enable_unicode_output()
    configure_logging(args.log_file, label="kiln-dashboard")

    ctx = DashboardContext(
        db_path=Path(args.db_path),
        branch=args.branch,
        status_dir=Path(args.status_dir),
        sessions_file=Path(args.sessions_file),
        project_name=args.project_name or Path.cwd().name,
        activity_limit=args.activity_limit,
    )

    while True:
        try:
            frame = snapshot(ctx)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            return 130
        except Exception:
            # A dashboard that dies over one bad poll takes the swarm-wide view with it.
            log.exception("render failed; continuing")
            frame = [f"{ICON_TITLE} Kiln Dashboard \N{EM DASH} render failed, retrying\N{HORIZONTAL ELLIPSIS}"]

        print(pane_status.CLEAR_SCREEN + "\n".join(frame), flush=True)

        if args.once:
            return 0
        try:
            time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            return 130


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
