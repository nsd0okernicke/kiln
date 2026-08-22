"""
The `/api/state` payload — pure builders over one gathered snapshot.

Everything here is a function from data to data: no clock, no database, no filesystem. The
snapshot arrives as `dashboard.SwarmSnapshot`, which is the same gathering the TTY dashboard
does, so the two views cannot report different numbers for the same run. That is the whole
reason `dashboard.collect` was split out of `dashboard.snapshot` — the ASCII renderer answers
in `list[str]`, which nothing can turn back into JSON.

Formatting decisions that already exist are reused rather than restated: `dashboard.format_age`
for ages, `dashboard.is_stalled` for the stall rule, `dashboard.cache_share` for the cache
ratio, `handoff.is_escalation` for escalations. A second definition of any of them is a
second thing to keep in sync with the dashboard, and the point of the split was that there
is only one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from scheduler import db, handoff
from scheduler.dashboard import (
    WORKING_STATES,
    SwarmSnapshot,
    attempt_suffix,
    cache_share,
    cost_is_partial,
    extract_summary,
    format_age,
    is_stalled,
    parse_local_timestamp,
    parse_status_since,
    render_totals,
    total_token_usage,
    visible_roles,
)
from scheduler.role_scheduler import is_pending

#: The lane a card reaches when nothing is holding it any more. Not a role, so it can never
#: collide with one: `render_board` puts it last and the page paints it differently.
LANE_DONE = "done"

#: How much of a handoff's opening prose a card shows. Longer than the dashboard's 60-column
#: budget because a browser card is not fighting a fixed-width grid for space.
CARD_SUMMARY_CHARS = 140

#: Shown on a card for a request that has neither a name nor readable prose to borrow.
UNNAMED_TITLE = "new request"


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
    return None if not work_item or is_pending(work_item) else work_item


@dataclass(frozen=True)
class CockpitContext:
    """The identity half of the payload — everything not read from disk each poll."""

    project_name: str
    branch: str
    #: Roles that get a swimlane, in board order. Empty means "infer from the traffic",
    #: which is what a cockpit started by hand outside a launch has to do.
    lanes: tuple[str, ...] = ()
    #: The role a human's queue belongs to. Messages waiting here are completed cycles
    #: asking for review, which is what the Attention rail is mostly made of.
    human_role: str = "human-in-the-loop"
    #: Where `New Task` sends. The routing table's answer for `human_role`, resolved at
    #: launch — the cockpit does not re-parse profiles.
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
    rows = []
    for session in visible_roles(snapshot.sessions):
        status = snapshot.statuses.get(session.role)
        queue = snapshot.queue_depth.get(session.role, 0)
        rows.append({
            "role": session.role,
            "agent": session.agent,
            "display_name": session.display_name,
            # The *resolved* model, written by the scheduler into its own status file --
            # the profile's value can be empty for a role whose model comes from the worker
            # definition's frontmatter. None until the role has reported once.
            "model": (status or {}).get("model"),
            "state": status["state"] if status else None,
            "since": (status or {}).get("since"),
            "since_ago": _since_ago(status, snapshot.now_utc),
            "stalled": is_stalled(status, snapshot.now_utc),
            "attempt": attempt_suffix(status).strip(),
            "queue": queue,
            "wait": _queue_wait(snapshot, session.role),
            "cycles": (status or {}).get("cycles"),
            "cost_usd": (status or {}).get("cost_usd"),
            "tokens": (status or {}).get("tokens"),
            "cache_share": cache_share((status or {}).get("token_usage")),
            "heat": activity_heat(status, queue),
            "work_item": holding.get(session.role),
        })
    return rows


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
    if row["status"] == db.STATUS_PROCESSED:
        return LANE_DONE
    return row["target"]


def build_board(
    work_items: list[dict], cycles: dict[str, int], now_local: datetime, lanes: tuple[str, ...]
) -> dict:
    """
    Cards grouped into swimlanes: `{"lanes": [...], "cards": {lane: [card, ...]}}`.

    `work_items` is `db.work_item_messages`' output — newest first, so the first row seen
    for a work item is its latest message and the rest are its history.
    """
    cards = [_card(row, cycles, now_local) for row in _latest_per_work_item(work_items)]
    order = list(lanes) or _observed_lanes(cards)
    if LANE_DONE not in order:
        order.append(LANE_DONE)

    grouped: dict[str, list[dict]] = {lane: [] for lane in order}
    for card in cards:
        # A card in a lane the board does not know about (a role that left the profile
        # between runs) is still real work and must not vanish; it gets its own lane at
        # the end rather than being dropped or silently folded into Done.
        grouped.setdefault(card["lane"], []).append(card)
    return {"lanes": list(grouped), "cards": grouped}


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
    items = [
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
    covered = {row["work_item"] for row in failed if row["work_item"]}

    items += [
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

    items += [
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
    return items


def build_activity(messages: list[dict], now_local: datetime, limit: int) -> list[dict]:
    """The recent handoff feed, same window the dashboard's activity list renders."""
    return [
        {
            "message_id": row["id"],
            "sender": row["sender"],
            "target": row["target"],
            "status": row["status"],
            "summary": extract_summary(row["content"], CARD_SUMMARY_CHARS),
            "created_at": row["created_at"],
            "age": _age(row["created_at"], now_local),
            "escalation": handoff.is_escalation(row["content"]),
        }
        for row in messages[:limit]
    ]


def build_totals(snapshot: SwarmSnapshot) -> dict:
    """Swarm-wide cost, cycles and tokens — `render_totals`' numbers, unrendered."""
    cost, cycles, tokens = render_totals(snapshot.statuses)
    return {
        "cost_usd": cost,
        # A swarm containing a role whose backend reports no cost has a structurally
        # incomplete total, not a small one. The page renders the `+` this flag earns.
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
) -> dict:
    """The whole `/api/state` document. Pure: every input is already gathered."""
    return {
        "project": ctx.project_name,
        "branch": ctx.branch,
        "human_role": ctx.human_role,
        "intake_role": ctx.intake_role,
        "generated_at": snapshot.now_local.isoformat(timespec="seconds"),
        "roles": role_rows(snapshot, work_items),
        "totals": build_totals(snapshot),
        "board": build_board(work_items, cycles, snapshot.now_local, ctx.lanes),
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

def _card(row: dict, cycles: dict[str, int], now_local: datetime) -> dict:
    work_item = named_work_item(row)
    summary = extract_summary(row["content"], CARD_SUMMARY_CHARS)
    return {
        "work_item": work_item,
        # What the card is called on screen. A named item is its own best label; an unnamed
        # one borrows the request's opening line, because "what did I ask for" is the only
        # thing that distinguishes two requests waiting to be specified.
        "title": work_item or summary or UNNAMED_TITLE,
        "unnamed": work_item is None,
        "lane": lane_for(row),
        "message_id": row["id"],
        "sender": row["sender"],
        "target": row["target"],
        "status": row["status"],
        "summary": summary,
        "created_at": row["created_at"],
        "age": _age(row["created_at"], now_local),
        # An unnamed request is exactly one message and nothing counts it: `work_item` is the
        # grouping key, so `cycles_by_work_item` has no entry to look up. Reporting the 0 that
        # lookup returns would put "0 msgs" on a card that visibly exists.
        "cycles": cycles.get(work_item, 0) if work_item else 1,
        "failed": row["status"] == db.STATUS_FAILED,
        "error": row.get("error"),
    }


def _latest_per_work_item(rows: list[dict]) -> list[dict]:
    """
    First row seen per work item, given newest-first input. Order is preserved.

    Unnamed rows are keyed by their own message id rather than by the `None` they all share:
    two people asking for two unrelated things are two cards, and grouping them under one
    absent name is the same mistake `work_item_of` exists to prevent in the database.

    An unnamed row that has been `processed` is dropped. The receiving role consumed it and
    handed on under a real name, so the named card now represents that work -- keeping the
    placeholder would strand a duplicate in **Done** describing work that is still moving.
    A `failed` one is kept: a request that stopped before it was ever named is precisely the
    thing a human has to see.
    """
    seen: set[str] = set()
    latest = []
    for row in rows:
        work_item = named_work_item(row)
        if work_item is None and row["status"] == db.STATUS_PROCESSED:
            continue
        key = work_item or f"id:{row['id']}"
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
    return latest


def _observed_lanes(cards: list[dict]) -> list[str]:
    """
    Lanes inferred from the cards themselves, for a cockpit started without a profile.

    A poor substitute for the profile's role list and knowingly so: a role that has not yet
    received anything has no lane, so the board grows one as work reaches it. Good enough to
    keep `python -m cockpit.server` useful on its own; the launcher always passes `--lanes`.
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
        if row["status"] in (db.STATUS_PROCESSED, db.STATUS_FAILED):
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

    Uses the **local** parser: `created_at` is naive localtime by the schema's own default,
    unlike the UTC `since` in the status files. Reading it with the wrong one would show
    every fresh message as hours old on any machine not running on UTC.
    """
    return _age(snapshot.oldest_queued.get(role), snapshot.now_local)


def _age(created_at: str | None, now_local: datetime) -> str | None:
    if not created_at:
        return None
    try:
        return format_age(parse_local_timestamp(created_at), now_local)
    except ValueError:
        return None
