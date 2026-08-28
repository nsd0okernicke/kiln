"""
The `/api/state` payload — pure builders over one gathered snapshot.

Everything here is a function from data to data: no clock, no database, no filesystem. The
snapshot arrives as `dashboard.SwarmSnapshot`, which is the same gathering the TTY dashboard
does, so the two views cannot report different numbers for the same run. That is the whole
reason `dashboard.collect` was split out of `dashboard.snapshot` — the ASCII renderer answers
in `list[str]`, which nothing can turn back into JSON.

The projection owns its JSON formatting rules and depends only on scheduler domain values.
The HTTP adapter gathers the concrete dashboard snapshot and SQLite rows, then passes those
plain values across this boundary.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from kiln.scheduler.domain import handoff
from kiln.scheduler.domain.models import MessageStatus
from kiln.scheduler.domain.status_contract import PENDING_HANDOFF

#: Terminal board lane for completed work.
LANE_DONE = "done"

#: Maximum handoff summary displayed on a card.
CARD_SUMMARY_CHARS = 140

#: Shown on a card for a request that has neither a name nor readable prose to borrow.
UNNAMED_TITLE = "new request"
WORKING_STATES = frozenset({"working", "retrying", "delegating"})
COST_REPORTING_AGENTS = frozenset({"claude", "grok"})
_SUMMARY_RE = re.compile(
    re.escape(handoff.SEPARATOR) + r".*\n" + re.escape(handoff.SEPARATOR) + r"\n(?P<summary>.*)",
    re.DOTALL,
)


class RoleSession(Protocol):
    role: str
    agent: str
    display_name: str
    model: str
    worktree: str
    passive: bool


class SwarmSnapshot(Protocol):
    sessions: list[RoleSession]
    statuses: dict[str, dict]
    queue_depth: dict[str, int]
    oldest_queued: dict[str, str]
    messages: list[dict]
    request_stats: dict[str, dict[str, int]]
    now_utc: datetime
    now_local: datetime


def visible_roles(sessions):
    return [session for session in sessions if not session.passive]


def extract_summary(content: str, max_chars: int = 60) -> str:
    match = _SUMMARY_RE.search(content)
    text = match.group("summary") if match else content
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    return line[: max_chars - 1] + "…" if len(line) > max_chars else line


def parse_status_since(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_local_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def format_age(value: datetime, now: datetime) -> str:
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def is_stalled(status: dict | None, now_utc: datetime) -> bool:
    if not status or status.get("state") not in WORKING_STATES:
        return False
    timeout, since = status.get("worker_timeout_sec"), status.get("since")
    if not timeout or not since:
        return False
    try:
        return (now_utc - parse_status_since(since)).total_seconds() > timeout
    except ValueError:
        return False


def attempt_suffix(status: dict | None) -> str:
    if not status:
        return ""
    attempt, limit = status.get("attempt"), status.get("max_attempts")
    return f" {attempt}/{limit}" if attempt and limit and attempt > 1 else ""


def cache_share(usage: dict | None) -> float | None:
    if not usage:
        return None
    total = sum(usage.values())
    return usage.get("cache_read", 0) / total if total > 0 else None


def render_totals(statuses: dict[str, dict]) -> tuple[float, int, int]:
    return (
        _sum_status(statuses, "cost_usd"),
        _sum_status(statuses, "cycles"),
        _sum_status(statuses, "tokens"),
    )


def _sum_status(statuses: dict[str, dict], field: str):
    return sum(status.get(field) or 0 for status in statuses.values())


def total_token_usage(statuses: dict[str, dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for status in statuses.values():
        for kind, count in (status.get("token_usage") or {}).items():
            totals[kind] = totals.get(kind, 0) + count
    return totals


def cost_is_partial(sessions, statuses: dict[str, dict]) -> bool:
    return any(
        session.agent not in COST_REPORTING_AGENTS and session.role in statuses
        for session in sessions
    )


def named_work_item(row: dict) -> str | None:
    """
    The row's work item, or None when it does not have one yet.

    Two ways a row has no name, and they must be treated identically. NULL is the normal one:
    `insert_handoff` stores nothing for an intake hop, because the specifier is what invents
    the name. The literal string `pending` is the other, and it is not hypothetical -- the
    `kiln-handoff` skill has wrapper-mode agents write this column by hand in raw SQL, so an
    agent that copies the placeholder through lands one here. Treating that as a real name
    would create a card called `pending` that every unrelated request then piles into, which
    is exactly the state `work_item_of` was written to prevent in the database.

    `is_pending` is reused rather than restated so there is one definition of the placeholder.
    """
    work_item = row.get("work_item")
    return None if not work_item or work_item.strip().lower() == PENDING_HANDOFF else work_item


@dataclass(frozen=True)
class CockpitContext:
    """The identity half of the payload — everything not read from disk each poll."""

    project_name: str
    branch: str
    #: Resolved Kiln framework version (e.g. ``"v0.4.0"``, ``"v0.4.0-12-gabc1234"``).
    version: str = "unknown"
    #: Board lanes in display order; empty infers lanes from traffic.
    lanes: tuple[str, ...] = ()
    #: The role a human's queue belongs to. Messages waiting here are completed cycles
    #: asking for review, which is what the Attention rail is mostly made of.
    human_role: str = "human-in-the-loop"
    #: Default destination when a human explicitly hands a backlog task off.
    intake_role: str = ""


def role_rows(snapshot: SwarmSnapshot, work_items: list[dict]) -> list[dict]:
    """
    One row per launched role: what it is doing, what is waiting for it, what it has spent.

    The Work Queue table. Ordering follows `.kiln/sessions`, which is profile order, so the
    table reads in the direction work actually flows instead of alphabetically.

    Stateless panes are left out through the same `visible_roles` the terminal dashboard
    uses -- `inbox`, `dashboard` and the cockpit itself report no state and contributed
    nothing but a row of dashes.
    """
    holding = _work_item_by_role(work_items)
    return [_role_row(snapshot, session, holding) for session in visible_roles(snapshot.sessions)]


def _role_row(snapshot: SwarmSnapshot, session: RoleSession, holding: dict[str, str]) -> dict:
    status = snapshot.statuses.get(session.role)
    values = status or {}
    queue = snapshot.queue_depth.get(session.role, 0)
    return {
        "role": session.role,
        "agent": session.agent,
        "display_name": session.display_name,
        # Status is authoritative because it includes worker-frontmatter model resolution.
        "model": _role_model(values, session),
        "worktree": _present(session.worktree),
        "state": status["state"] if status else None,
        "since": values.get("since"),
        "since_ago": _since_ago(status, snapshot.now_utc),
        "stalled": is_stalled(status, snapshot.now_utc),
        "attempt": attempt_suffix(status).strip(),
        "queue": queue,
        "wait": _queue_wait(snapshot, session.role),
        "cycles": values.get("cycles"),
        "cost_usd": values.get("cost_usd"),
        "tokens": values.get("tokens"),
        "token_usage": _token_usage(values),
        "cache_share": cache_share(values.get("token_usage")),
        "worker_timeout_sec": values.get("worker_timeout_sec"),
        "heat": activity_heat(status, queue),
        "work_item": holding.get(session.role),
    }


def _role_model(values: dict, session: RoleSession) -> str | None:
    return values.get("model") or session.model or None


def _present(value: str) -> str | None:
    return value or None


def _token_usage(values: dict) -> dict:
    return values.get("token_usage") or {}


def activity_heat(status: dict | None, queue_depth: int) -> float:
    """
    How busy this role is right now, on a deliberately coarse 0 / 0.5 / 1 scale.

    Discrete rather than a gradient because there is nothing to make a gradient out of. The
    obvious candidate -- how long the role has been in its current state -- runs backwards:
    a role nine minutes into a worker invocation is *more* engaged than one that started ten
    seconds ago, not cooler. A smooth-looking number derived from the wrong quantity is worse
    than three honest levels, so:

    * 1.0 — a worker is running (`working`, `retrying`, `delegating`).
    * 0.5 — idle with something queued: work has arrived and is not moving yet.
    * 0.0 — idle and empty, or no status file at all.
    """
    state = (status or {}).get("state")
    if state in WORKING_STATES:
        return 1.0
    return 0.5 if queue_depth else 0.0


def lane_for(row: dict) -> str:
    """
    Which swimlane one work item's latest message puts it in.

    Three cases, and they are exhaustive because a message has exactly one status:

    * `failed` — the cycle escalated. The card stays in the lane of the role that failed,
      because that is where a retry sends it back to; the Attention rail is what shouts.
    * `processed` — the receiving role has consumed the message and queued nothing after it
      (any handoff it sent would itself be the latest message and would be examined
      instead). Nothing holds the item, so it is done.
    * anything else (`queued`, `delivered`, `processing`) — the target holds it.

    Deliberately no role names appear here. The rule "done when architect -> human closed the
    cycle" is true of the shipped `full` profile and false of every profile with a different
    shape; "done when the last message was consumed and nothing followed" is true of all of
    them and needs no routing table to evaluate.
    """
    if row["status"] == MessageStatus.PROCESSED:
        return LANE_DONE
    return row["target"]


def build_board(
    work_items: list[dict],
    cycles: dict[str, int],
    now_local: datetime,
    lanes: tuple[str, ...],
    tasks: Sequence[dict] = (),
    human_role: str = "human-in-the-loop",
) -> dict:
    """
    Cards grouped into swimlanes: `{"lanes": [...], "cards": {lane: [card, ...]}}`.

    `work_items` is `db.work_item_messages`' output — newest first. An active row outranks a
    later processed escalation because retry reactivates the original row without changing
    its creation time.
    """
    task_titles = {task["work_item"]: task["title"] for task in tasks}
    cycle_durations = _cycle_durations(work_items)
    cards = [
        _backlog_card(task, now_local, human_role) for task in tasks if task["status"] == "backlog"
    ]
    cards += [
        _card(row, cycles, now_local, task_titles, cycle_durations)
        for row in _latest_per_work_item(work_items)
    ]
    order = _board_lane_order(lanes, cards, human_role)
    grouped = _cards_by_lane(cards, order)
    return {"lanes": list(grouped), "cards": grouped}


def _board_lane_order(lanes: tuple[str, ...], cards: list[dict], human_role: str) -> list[str]:
    order = list(lanes) or _observed_lanes(cards)
    if human_role not in order:
        order.insert(0, human_role)
    if LANE_DONE not in order:
        order.append(LANE_DONE)
    return order


def _cards_by_lane(cards: list[dict], order: list[str]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {lane: [] for lane in order}
    for card in cards:
        # Preserve work for roles removed from the current profile.
        grouped.setdefault(card["lane"], []).append(card)
    return grouped


def build_attention(
    *,
    failed: list[dict],
    awaiting_human: list[dict],
    messages: list[dict],
    now_local: datetime,
    human_role: str,
) -> list[dict]:
    """
    What a human has to look at, most urgent first.

    Two sources, and they are different things. A `failed` row is a cycle that stopped: the
    scheduler gave up, the work is parked, and nothing moves until someone retries it. A
    message queued for the human is a cycle that *succeeded* and wants review. Sorting the
    first kind ahead of the second is the whole ranking — a stopped swarm outranks a finished
    one.

    Escalation messages in the recent window are folded in as `escalation` rows only when no
    `failed` row already covers the same work item: `_escalate` writes both, and showing them
    twice would double-count the swarm's only genuinely alarming signal.
    """
    items = _failed_attention(failed, now_local)
    covered = {row["work_item"] for row in failed if row["work_item"]}
    items += _escalation_attention(messages, covered, now_local)
    items += _review_attention(awaiting_human, human_role, now_local)
    return items


def _failed_attention(failed: list[dict], now_local: datetime) -> list[dict]:
    return [
        {
            "kind": "failed",
            "message_id": row["id"],
            "role": row["target"],
            "sender": row["sender"],
            "work_item": row["work_item"],
            "summary": row["error"] or "(no reason recorded)",
            "created_at": row["created_at"],
            "age": _age(row["created_at"], now_local),
            "retryable": True,
        }
        for row in failed
    ]


def _escalation_attention(
    messages: list[dict], covered: set[str], now_local: datetime
) -> list[dict]:
    return [
        {
            "kind": "escalation",
            "message_id": row["id"],
            "role": row["target"],
            "sender": row["sender"],
            "work_item": row.get("work_item"),
            "summary": extract_summary(row["content"], CARD_SUMMARY_CHARS),
            "created_at": row["created_at"],
            "age": _age(row["created_at"], now_local),
            "retryable": False,
        }
        for row in messages
        if handoff.is_escalation(row["content"]) and row.get("work_item") not in covered
    ]


def _review_attention(
    awaiting_human: list[dict], human_role: str, now_local: datetime
) -> list[dict]:
    return [
        {
            "kind": "review",
            "message_id": row["id"],
            "role": human_role,
            "sender": row["sender"],
            "work_item": row["work_item"],
            "summary": extract_summary(row["content"], CARD_SUMMARY_CHARS),
            "created_at": row["created_at"],
            "age": _age(row["created_at"], now_local),
            "retryable": False,
        }
        for row in awaiting_human
    ]


def build_activity(messages: list[dict], now_local: datetime, limit: int) -> list[dict]:
    """The recent handoff feed, same window the dashboard's activity list renders."""
    return [
        {
            "message_id": row["id"],
            "sender": row["sender"],
            "target": row["target"],
            "status": _activity_status(row),
            "summary": extract_summary(row["content"], CARD_SUMMARY_CHARS),
            "created_at": row["created_at"],
            "age": _age(row["created_at"], now_local),
            "escalation": handoff.is_escalation(row["content"]),
        }
        for row in messages[:limit]
    ]


def _activity_status(row: dict) -> str:
    """Distinguish a human-resumed attempt from the row's ordinary processing lifecycle."""
    if row.get("acked_at") and row["status"] in {
        MessageStatus.QUEUED,
        MessageStatus.DELIVERED,
        MessageStatus.PROCESSING,
    }:
        return "retrying"
    return row["status"]


def build_totals(snapshot: SwarmSnapshot) -> dict:
    """Swarm-wide cost, cycles and tokens — `render_totals`' numbers, unrendered."""
    cost, cycles, tokens = render_totals(snapshot.statuses)
    return {
        "cost_usd": cost,
        "cost_partial": cost_is_partial(snapshot.sessions, snapshot.statuses),
        "cycles": cycles,
        "tokens": tokens,
        "token_usage": total_token_usage(snapshot.statuses),
    }


def build_state(
    ctx: CockpitContext,
    snapshot: SwarmSnapshot,
    *,
    work_items: list[dict],
    cycles: dict[str, int],
    failed: list[dict],
    awaiting_human: list[dict],
    activity_limit: int,
    tasks: Sequence[dict] = (),
    sequential: bool = False,
) -> dict:
    """The whole `/api/state` document. Pure: every input is already gathered."""
    return {
        "project": ctx.project_name,
        "version": ctx.version,
        "branch": ctx.branch,
        "human_role": ctx.human_role,
        "intake_role": ctx.intake_role,
        "generated_at": snapshot.now_local.isoformat(timespec="seconds"),
        "roles": role_rows(snapshot, work_items),
        "sequential": sequential,
        "work_items": list(
            dict.fromkeys(
                [task["work_item"] for task in tasks if task["status"] != "archived"] + list(cycles)
            )
        ),
        "totals": build_totals(snapshot),
        "board": build_board(
            work_items,
            cycles,
            snapshot.now_local,
            ctx.lanes,
            tasks=tasks,
            human_role=ctx.human_role,
        ),
        "attention": build_attention(
            failed=failed,
            awaiting_human=awaiting_human,
            messages=snapshot.messages,
            now_local=snapshot.now_local,
            human_role=ctx.human_role,
        ),
        "activity": build_activity(snapshot.messages, snapshot.now_local, activity_limit),
        "request_stats": snapshot.request_stats,
    }


# --- internals ---------------------------------------------------------------------


def _card(
    row: dict,
    cycles: dict[str, int],
    now_local: datetime,
    task_titles: dict[str, str],
    cycle_durations: dict[str, str],
) -> dict:
    work_item = named_work_item(row)
    summary = extract_summary(row["content"], CARD_SUMMARY_CHARS)
    lane = lane_for(row)
    return {
        "work_item": work_item,
        "title": _card_title(work_item, summary, task_titles),
        "unnamed": work_item is None,
        "lane": lane,
        "message_id": row["id"],
        "sender": row["sender"],
        "target": row["target"],
        "status": row["status"],
        "summary": summary,
        "created_at": row["created_at"],
        "age": _age(row["created_at"], now_local),
        "duration": _finished_duration(lane, work_item, cycle_durations),
        "cycles": _cycle_count(work_item, cycles),
        "failed": row["status"] == MessageStatus.FAILED,
        "error": row.get("error"),
        "kind": "handoff",
    }


def _card_title(work_item: str | None, summary: str, task_titles: dict[str, str]) -> str:
    return task_titles.get(work_item or "", "") or summary or work_item or UNNAMED_TITLE


def _finished_duration(
    lane: str, work_item: str | None, cycle_durations: dict[str, str]
) -> str | None:
    return cycle_durations.get(work_item or "") if lane == LANE_DONE else None


def _cycle_count(work_item: str | None, cycles: dict[str, int]) -> int:
    return cycles.get(work_item, 0) if work_item else 1


def _backlog_card(task: dict, now_local: datetime, human_role: str) -> dict:
    return {
        "work_item": task["work_item"],
        "title": task["title"],
        "unnamed": False,
        "lane": human_role,
        "task_id": task["id"],
        "message_id": None,
        "sender": human_role,
        "target": human_role,
        "status": task["status"],
        "summary": task["body"][:CARD_SUMMARY_CHARS],
        "body": task["body"],
        "created_at": task["created_at"],
        "age": _age(task["updated_at"], now_local),
        "cycles": 0,
        "failed": False,
        "error": None,
        "kind": "backlog",
    }


def _latest_per_work_item(rows: list[dict]) -> list[dict]:
    """
    Current row per work item, preferring active work and otherwise the newest history row.

    Unnamed rows are keyed by their own message id rather than by the `None` they all share:
    two people asking for two unrelated things are two cards, and grouping them under one
    absent name is the same mistake `work_item_of` exists to prevent in the database.

    An unnamed row that has been `processed` is dropped. The receiving role consumed it and
    handed on under a real name, so the named card now represents that work -- keeping the
    placeholder would strand a duplicate in **Done** describing work that is still moving.
    A `failed` one is kept: a request that stopped before it was ever named is precisely the
    thing a human has to see.
    """
    active_by_item = _active_work_items(rows)
    seen: set[str] = set()
    latest = []
    for row in rows:
        work_item = named_work_item(row)
        if _superseded_board_row(row, work_item, active_by_item):
            continue
        key = work_item or f"id:{row['id']}"
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
    return latest


def _active_work_items(rows: list[dict]) -> set[str]:
    active = set()
    for row in rows:
        work_item = named_work_item(row)
        if work_item is not None and row["status"] != MessageStatus.PROCESSED:
            active.add(work_item)
    return active


def _superseded_board_row(row: dict, work_item: str | None, active: set[str]) -> bool:
    if row["status"] != MessageStatus.PROCESSED:
        return False
    if work_item is None:
        return True
    return work_item in active


def _observed_lanes(cards: list[dict]) -> list[str]:
    """
    Lanes inferred from the cards themselves, for a cockpit started without a profile.

    A poor substitute for the profile's role list and knowingly so: a role that has not yet
    received anything has no lane, so the board grows one as work reaches it. Good enough to
    keep the standalone HTTP server useful; the launcher always passes `--lanes`.
    """
    order: list[str] = []
    for card in cards:
        if card["lane"] != LANE_DONE and card["lane"] not in order:
            order.append(card["lane"])
    return order


def _work_item_by_role(work_items: list[dict]) -> dict[str, str]:
    """
    What each role is currently holding: its newest still-unfinished inbound message.

    Read from the queue rather than the status file because the status file has no work-item
    field — `set-status.py` writes state, cost and tokens, and nothing has ever told it which
    piece of work those belong to.

    An unnamed request falls back to its opening line, so the column says what the specifier
    is working on rather than going blank during the minutes before the work has a name.
    Written with an explicit membership check rather than `setdefault`: unnamed rows now reach
    this loop, and `setdefault` keys on presence, so the first one would claim the role and
    then refuse to be replaced by the named message that follows it.
    """
    holding: dict[str, str] = {}
    for row in work_items:
        if row["status"] in (MessageStatus.PROCESSED, MessageStatus.FAILED):
            continue
        if row["target"] in holding:
            continue
        label = named_work_item(row) or extract_summary(row["content"], CARD_SUMMARY_CHARS)
        holding[row["target"]] = label or UNNAMED_TITLE
    return holding


def _since_ago(status: dict | None, now_utc: datetime) -> str | None:
    if not status or not status.get("since"):
        return None
    try:
        return format_age(parse_status_since(status["since"]), now_utc)
    except ValueError:
        return None


def _queue_wait(snapshot: SwarmSnapshot, role: str) -> str | None:
    """
    How long this role's oldest queued message has waited.

    Current values are UTC ISO timestamps. `parse_local_timestamp` also accepts legacy
    naive-local database values and normalizes both to the local display clock.
    """
    return _age(snapshot.oldest_queued.get(role), snapshot.now_local)


def _age(created_at: str | None, now_local: datetime) -> str | None:
    if not created_at:
        return None
    try:
        return format_age(parse_local_timestamp(created_at), now_local)
    except ValueError:
        return None


def _cycle_durations(rows: list[dict]) -> dict[str, str]:
    seconds_by_item: dict[str, int] = {}
    for row in rows:
        work_item = named_work_item(row)
        elapsed = _elapsed_seconds(row)
        if not work_item or elapsed is None:
            continue
        seconds_by_item[work_item] = seconds_by_item.get(work_item, 0) + elapsed
    return {work_item: format_duration(seconds) for work_item, seconds in seconds_by_item.items()}


def _elapsed_seconds(row: dict) -> int | None:
    started_at, finished_at = row.get("started_at"), row.get("finished_at")
    if not started_at or not finished_at:
        return None
    try:
        elapsed = parse_local_timestamp(finished_at) - parse_local_timestamp(started_at)
    except ValueError:
        return None
    return max(0, int(elapsed.total_seconds()))
