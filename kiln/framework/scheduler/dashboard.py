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
from datetime import UTC, datetime
from pathlib import Path

from . import db, handoff, pane_status
from .role_scheduler import configure_logging, enable_unicode_output

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 2.0
DEFAULT_ACTIVITY_LIMIT = 8

ICON_TITLE = "\N{BAR CHART}"

#: Everything after a handoff message's closing banner rule, up to the next blank line --
#: matches `handoff.format_handoff`'s layout exactly (SEPARATOR, banner, SEPARATOR, summary).
_SEPARATOR_RE = re.escape(handoff.SEPARATOR)
_SUMMARY_RE = re.compile(
    _SEPARATOR_RE + r".*\n" + _SEPARATOR_RE + r"\n(?P<summary>.*)", re.DOTALL
)


#: Pane kinds that run no agent and report no state.
#:
#: Nothing ever hands these panes a `--status-script`, so they never write
#: `.kiln/status/<role>.json` -- not "have not yet", but structurally cannot. Listing them
#: beside roles that do report state produced a row of dashes in every view, and in WezTerm
#: something worse: the tab-bar Lua defaults a `manual` role with no status file to
#: `waiting`, so a perfectly healthy cockpit pane advertised itself as waiting for something.
#:
#: Restated here rather than imported: `launcher.config` owns these strings and writes them
#: into the sessions file, but `scheduler` must not import `launcher` -- the dependency runs
#: the other way. `test_dashboard` pins the two sets against each other, which a test can do
#: because it may import both.
PASSIVE_KINDS = frozenset({"inbox", "dashboard", "cockpit"})

#: What a pane whose kind the sessions file does not record is assumed to be. Only a file
#: written before the kind column existed can produce this, and treating those roles as
#: agents keeps an older launch rendering exactly as it used to.
DEFAULT_KIND = "agent"


@dataclass(frozen=True)
class RoleSession:
    """
    One row of `.kiln/sessions` — the static role inventory
    `workspace.write_sessions_file` writes at launch.
    """

    role: str
    agent: str
    display_name: str
    #: `agent`, `python`, `inbox`, `dashboard` or `cockpit` — the profile's `scheduler` value.
    kind: str = DEFAULT_KIND

    @property
    def passive(self) -> bool:
        """True for a pane that runs no agent and can never report state."""
        return self.kind in PASSIVE_KINDS


def read_sessions(path: Path) -> list[RoleSession]:
    """
    Parse `.kiln/sessions` (`index\\trole\\tagent\\tdisplay_name\\tkind` per line).

    **Every row is returned, passive ones included.** Filtering belongs to the renderers:
    `launcher.cli.run_stop` and `cockpit.actions._session_roles` both read this to close
    tmux sessions, and a passive pane dropped here would be one nothing ever tore down.

    The kind column is optional so a sessions file from an earlier launch -- one written
    before the column existed, which is exactly what a running swarm has on disk during an
    upgrade -- still parses, with every role defaulting to `agent`.
    """
    if not path.is_file():
        return []
    sessions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        _, role, agent, display_name = parts[:4]
        kind = parts[4].strip() if len(parts) > 4 and parts[4].strip() else DEFAULT_KIND
        sessions.append(
            RoleSession(role=role, agent=agent, display_name=display_name, kind=kind)
        )
    return sessions


def visible_roles(sessions: list[RoleSession]) -> list[RoleSession]:
    """
    The roles worth a row in a state table — everything except the stateless panes.

    One rule with two callers (this module's grid and `cockpit.state.role_rows`), so the
    terminal and the browser cannot come to disagree about which roles exist.
    """
    return [session for session in sessions if not session.passive]


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


def parse_status_since(value: str) -> datetime:
    """`set-status.py` writes UTC ISO 8601 with a trailing `Z`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_local_timestamp(value: str) -> datetime:
    """`messages.db`'s `created_at` is naive local time (the schema's own default)."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def format_age(dt: datetime, now: datetime) -> str:
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


#: States in which a role is waiting on a worker subprocess, and therefore the only ones a
#: stall means anything in. An `idle` role with an old `since` is not stuck, it is unemployed.
WORKING_STATES = frozenset({"working", "retrying", "delegating"})

#: Marks a role whose worker has run longer than the timeout it was launched with.
STALL_MARKER = "\N{WARNING SIGN}"


def is_stalled(status: dict | None, now_utc: datetime) -> bool:
    """
    True when a role has been working longer than its own configured worker timeout.

    A role hung in `working` for an hour renders identically to one working normally, just
    with a larger SINCE -- and "larger" is only meaningful against a number the dashboard did
    not have. The timeout arrives in the status file, written by the scheduler that owns it,
    so this compares against what the role was actually launched with rather than against a
    profile the dashboard would have to re-parse and could disagree about.

    Unknowable cases are not stalls: no status file, no timeout recorded, no `since`, or a
    state where no worker is running.
    """
    if not status or status.get("state") not in WORKING_STATES:
        return False
    timeout = status.get("worker_timeout_sec")
    since = status.get("since")
    if not timeout or not since:
        return False
    try:
        elapsed = (now_utc - parse_status_since(since)).total_seconds()
    except ValueError:
        return False
    return elapsed > timeout


def attempt_suffix(status: dict | None) -> str:
    """`2/2` for a role on its retry, or '' when it is on its first (or never reported)."""
    if not status:
        return ""
    attempt, limit = status.get("attempt"), status.get("max_attempts")
    if not attempt or not limit or attempt <= 1:
        return ""
    return f" {attempt}/{limit}"


def render_state_grid(
    sessions: list[RoleSession],
    statuses: dict[str, dict],
    queue_depth: dict[str, int],
    now_utc: datetime,
    oldest_queued: dict[str, str] | None = None,
    now_local: datetime | None = None,
) -> list[str]:
    header = (
        f"{'ROLE':<20} {'STATE':<20} {'SINCE':<12} {'QUEUE':>5} {'WAIT':>8} {'CYCLES':>7} "
        f"{'COST':>8} {'TOKENS':>9} {'CACHE':>6}"
    )
    lines = [header, "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * len(header)]
    # Stateless panes are skipped here rather than by the caller: `render_dashboard` also
    # hands `sessions` to `cost_is_partial`, which needs the full inventory to judge whether
    # a non-cost-reporting backend is in play.
    for session in visible_roles(sessions):
        status = statuses.get(session.role)
        state = status["state"] if status else "-"
        since = (
            format_age(parse_status_since(status["since"]), now_utc)
            if status and status.get("since")
            else "-"
        )
        if is_stalled(status, now_utc):
            since = f"{STALL_MARKER} {since}"
        cycles = status.get("cycles") if status else None
        cost = status.get("cost_usd") if status else None
        tokens = status.get("tokens") if status else None
        label = f"{pane_status.STATE_GLYPH} {state}{attempt_suffix(status)}"
        state_cell = _colorize(label.ljust(20), state)
        cycles_display = "-" if cycles is None else str(cycles)
        cost_display = "-" if cost is None else f"${cost:.2f}"
        # `-` for both "no status file yet" and "this backend reported no usage" -- the same
        # treatment CYCLES/COST already give a role that never tracked them.
        tokens_display = "-" if not tokens else pane_status.format_tokens(tokens)
        share = cache_share(status.get("token_usage") if status else None)
        cache_display = "-" if share is None else f"{share:.0%}"
        lines.append(
            f"{session.role:<20} {state_cell} {since:<12} "
            f"{queue_depth.get(session.role, 0):>5} "
            f"{queue_wait(oldest_queued, session.role, now_local):>8} "
            f"{cycles_display:>7} {cost_display:>8} "
            f"{tokens_display:>9} {cache_display:>6}"
        )
    return lines


def queue_wait(
    oldest_queued: dict[str, str] | None, role: str, now_local: datetime | None
) -> str:
    """
    How long this role's oldest queued message has waited, or `-` when nothing is waiting.

    Read with the *local* parser: `created_at` is naive localtime by the schema's own default,
    unlike the UTC `since` in the status files. `dashboard` already carries both parsers for
    exactly this reason, and using the wrong one here would show every fresh message as hours
    old on any machine not on UTC.
    """
    stamp = (oldest_queued or {}).get(role)
    if not stamp or now_local is None:
        return "-"
    try:
        return format_age(parse_local_timestamp(stamp), now_local).removesuffix(" ago")
    except ValueError:
        return "-"


def cache_share(usage: dict | None) -> float | None:
    """
    Fraction of a role's tokens served from cache, or None when unknown.

    This is the column worth watching. Token counts alone do not explain cost: a role can
    burn millions of tokens cheaply if nearly all of them are cache reads, while another
    burning far fewer can cost more because each cycle re-sends an uncached prompt. The
    ratio is what points at prompt bloat, which is the whole point of measuring.

    None (rendered `-`) rather than 0% when there is no breakdown: a backend that reported
    no usage has not told us its cache rate is zero.
    """
    if not usage:
        return None
    total = sum(usage.values())
    if total <= 0:
        return None
    return usage.get("cache_read", 0) / total


#: Backends whose one-shot adapter reports real dollars. Copilot and Codex report token
#: usage but no cost at all (see their module docstrings), so a swarm containing either has
#: a TOTAL COST that is structurally incomplete rather than merely small -- which looked
#: identical to a genuinely cheap run until this marker existed.
COST_REPORTING_AGENTS = frozenset({"claude", "grok"})


def cost_is_partial(sessions: list[RoleSession], statuses: dict[str, dict]) -> bool:
    """True when some role that has actually run cannot contribute cost."""
    return any(
        session.agent not in COST_REPORTING_AGENTS and session.role in statuses
        for session in sessions
    )


def render_totals(statuses: dict[str, dict]) -> tuple[float, int, int]:
    total_cost = sum(status.get("cost_usd") or 0 for status in statuses.values())
    total_cycles = sum(status.get("cycles") or 0 for status in statuses.values())
    total_tokens = sum(status.get("tokens") or 0 for status in statuses.values())
    return total_cost, total_cycles, total_tokens


def total_token_usage(statuses: dict[str, dict]) -> dict[str, int]:
    """Swarm-wide token breakdown, summed per kind across every role that reported one."""
    totals: dict[str, int] = {}
    for status in statuses.values():
        for kind, count in (status.get("token_usage") or {}).items():
            totals[kind] = totals.get(kind, 0) + count
    return totals


def format_token_breakdown(totals: dict[str, int]) -> str:
    """`in 120k · out 45k · cache-read 8.8M` -- omits kinds nobody reported."""
    labels = (("input", "in"), ("output", "out"), ("cache_read", "cache-read"),
              ("cache_write", "cache-write"))
    parts = [
        f"{label} {pane_status.format_tokens(totals[kind]).removesuffix(' tok')}"
        for kind, label in labels
        if totals.get(kind)
    ]
    return " \N{MIDDLE DOT} ".join(parts)


def _format_bytes(count: int) -> str:
    """`104.2k` / `1.2M` — request sizes are read at a glance, not to the byte."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def read_request_stats(
    traffic_db: Path | None, since: str | None = None
) -> dict[str, dict[str, int]]:
    """
    Per-role prompt weight from the proxy's capture store, or `{}` when there is none.

    Optional by design: the proxy is opt-in, so every failure mode here — no file, no
    rows, an unreadable store — collapses to "render no panel" rather than an error. The
    dashboard's job is the swarm, and it must not die over a side channel.
    """
    if not traffic_db:
        return {}
    try:
        from proxy.capture import TrafficStore

        return TrafficStore(traffic_db).request_stats_by_role(since=since)
    except Exception:  # pragma: no cover - optional panel, never fatal
        log.debug("could not read traffic store at %s", traffic_db, exc_info=True)
        return {}


def render_prompt_weight(
    stats: dict[str, dict[str, int]], scope: str = "this run"
) -> list[str]:
    """
    What each role actually puts on the wire — the number Phase A cannot produce.

    Token counts say a role is expensive; request size says *why*. A role whose every call
    carries a six-figure payload is re-sending context it could be caching or trimming,
    and AVG against MAX says whether that is every call or a single outlier.
    """
    if not stats:
        return []
    header = (
        f"{'ROLE':<20} {'REQS':>6} {'AVG REQ':>9} {'MAX REQ':>9} "
        f"{'TOOLS':>8} {'SYSTEM':>8} {'MSGS':>8} {'MSG%':>5}"
    )
    # The scope is stated, not implied: the store outlives a run, so "is this the current
    # configuration or an average across every run in the file" must never be a guess.
    lines = ["", f"Prompt weight (proxy)  \N{EM DASH} averages per request, {scope}", header]
    for role, entry in sorted(stats.items()):
        messages = entry.get("avg_messages")
        share = (
            f"{100 * messages / entry['avg_bytes']:.0f}%"
            if messages and entry.get("avg_bytes")
            else "-"
        )
        lines.append(
            f"{role:<20} {entry['requests']:>6} "
            f"{_format_bytes(entry['avg_bytes']):>9} "
            f"{_format_bytes(entry['max_bytes']):>9} "
            f"{_section(entry.get('avg_tools')):>8} "
            f"{_section(entry.get('avg_system')):>8} "
            f"{_section(messages):>8} {share:>5}"
        )
    return lines


def _section(value: int | None) -> str:
    """A composition column: `-` when nothing recorded it, never a misleading 0."""
    return "-" if value is None else _format_bytes(value)


def _activity_line(row: dict, now_local: datetime) -> str:
    when = format_age(parse_local_timestamp(row["created_at"]), now_local)
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
    request_stats: dict[str, dict[str, int]] | None = None,
    request_scope: str = "this run",
    oldest_queued: dict[str, str] | None = None,
) -> list[str]:
    """Pure: given one snapshot of the world, render every section. No I/O, no clock."""
    header = f"{ICON_TITLE} Kiln Dashboard \N{EM DASH} {project_name} ({branch})"
    timestamp = now_local.strftime("%H:%M:%S")
    padding = max(2, 74 - len(header) - len(timestamp))
    title_line = f"{header}{' ' * padding}{timestamp}"

    grid = render_state_grid(
        sessions, statuses, queue_depth, now_utc,
        oldest_queued=oldest_queued, now_local=now_local,
    )
    # Sized to the widest thing it has to underline, not to the title alone: the grid grew
    # past a title-width rule when the TOKENS column was added, leaving the table visibly
    # overhanging its own borders. `grid[0]` is the column header, the one grid line
    # carrying no ANSI colour codes and therefore the only one whose len() is its width.
    rule = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * max(len(title_line), len(grid[0]))

    lines = [title_line, rule]
    lines += grid
    lines.append(rule)

    total_cost, total_cycles, total_tokens = render_totals(statuses)
    escalation_count = sum(1 for row in messages if handoff.is_escalation(row["content"]))
    cost_display = f"${total_cost:.2f}"
    if cost_is_partial(sessions, statuses):
        cost_display += "+"
    lines.append(
        f"TOTAL COST: {cost_display}        TOTAL CYCLES: {total_cycles}        "
        f"TOKENS: {pane_status.format_tokens(total_tokens)}        "
        f"ESCALATIONS: {escalation_count}"
    )
    breakdown = format_token_breakdown(total_token_usage(statuses))
    if breakdown:
        lines.append(f"  tokens by kind: {breakdown}")
    if cost_is_partial(sessions, statuses):
        lines.append(
            "  + partial: codex/copilot roles report tokens but no cost"
        )
    # Only when it applies: a permanent legend for a condition that is usually absent trains
    # people to stop reading the line, which is the opposite of an early warning.
    stalled = [s.role for s in sessions if is_stalled(statuses.get(s.role), now_utc)]
    if stalled:
        lines.append(
            f"  {STALL_MARKER} past its worker timeout, so the worker may be hung: "
            + ", ".join(stalled)
        )

    lines += render_prompt_weight(request_stats or {}, scope=request_scope)
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
    #: The proxy's capture store. None, or a path that does not exist, simply hides the
    #: prompt-weight panel — the proxy is opt-in and the dashboard works without it.
    traffic_db: Path | None = None
    #: Ignore captured traffic older than this ISO-8601 UTC timestamp. Defaults to the
    #: dashboard's own start, which is launched with the swarm and so approximates "this
    #: run". None means every row in the store, however old.
    traffic_since: str | None = None


@dataclass(frozen=True)
class SwarmSnapshot:
    """
    One gathered view of the world, before anything decides how to display it.

    Split out of `snapshot` so a second front end can read the same numbers the TTY
    dashboard shows without going through `render_dashboard`, which answers in rendered
    ASCII (`list[str]`) and therefore cannot be re-parsed into anything else. The web
    cockpit (`cockpit.state`) builds its JSON from this; the ASCII render is unchanged and
    still the only consumer inside this module.

    Both clocks travel with the data deliberately: `created_at` in the queue is naive
    localtime and `since` in the status files is UTC, so a consumer that kept only one of
    them would necessarily read one of the two wrong -- the exact bug `queue_wait` documents.
    """

    sessions: list[RoleSession]
    statuses: dict[str, dict]
    queue_depth: dict[str, int]
    oldest_queued: dict[str, str]
    messages: list[dict]
    request_stats: dict[str, dict[str, int]]
    now_utc: datetime
    now_local: datetime


def collect(ctx: DashboardContext) -> SwarmSnapshot:
    """Read every source of truth once: sessions, status files, queue, recent messages."""
    sessions = read_sessions(ctx.sessions_file)
    statuses = {
        session.role: status
        for session in sessions
        if (status := read_status(ctx.status_dir, session.role)) is not None
    }
    return SwarmSnapshot(
        sessions=sessions,
        statuses=statuses,
        queue_depth=db.count_queued_by_role(ctx.db_path, ctx.branch),
        oldest_queued=db.oldest_queued_by_role(ctx.db_path, ctx.branch),
        # A wider window than activity_limit so escalations outside the visible activity
        # list still have a chance to surface -- this is a recent-window view, not
        # exhaustive history.
        messages=db.recent_messages(
            ctx.db_path, ctx.branch, limit=max(ctx.activity_limit, 20)
        ),
        request_stats=read_request_stats(ctx.traffic_db, since=ctx.traffic_since),
        now_utc=datetime.now(UTC),
        now_local=datetime.now(),
    )


def snapshot(ctx: DashboardContext) -> list[str]:
    """Gather current state from disk/DB and render one frame."""
    state = collect(ctx)
    return render_dashboard(
        project_name=ctx.project_name,
        branch=ctx.branch,
        sessions=state.sessions,
        statuses=state.statuses,
        queue_depth=state.queue_depth,
        messages=state.messages,
        now_utc=state.now_utc,
        now_local=state.now_local,
        activity_limit=ctx.activity_limit,
        request_stats=state.request_stats,
        request_scope="this run" if ctx.traffic_since else "all history",
        oldest_queued=state.oldest_queued,
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
    parser.add_argument(
        "--traffic-db", default=None,
        help="proxy capture store; the prompt-weight panel is hidden when absent",
    )
    parser.add_argument(
        "--traffic-all-history", action="store_true",
        help=(
            "include captured traffic from previous runs. Off by default: the store "
            "outlives a run, and averaging across runs blends configurations that are not "
            "comparable"
        ),
    )
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
        traffic_db=Path(args.traffic_db) if args.traffic_db else None,
        # Stamped once at startup, not per frame: the window must not creep forward as the
        # run proceeds, or early requests would silently drop out of the averages.
        traffic_since=(
            None if args.traffic_all_history
            else datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ),
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
            frame = [
                f"{ICON_TITLE} Kiln Dashboard \N{EM DASH} render failed, "
                f"retrying\N{HORIZONTAL ELLIPSIS}"
            ]

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
