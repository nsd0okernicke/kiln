"""
The cockpit's write half — everything a button does.

Not one line of queue, retry or teardown logic lives here. `scheduler.send.send`,
`scheduler.retry.resume` and `launcher.stop.stop_all` already own those decisions and are
already the paths `kiln send` / `kiln retry` / `kiln --stop` take, so the cockpit calls them
and does nothing else. A second implementation of "queue a handoff" is how the browser and
the CLI would come to disagree about what a handoff is.

**Where a new task goes.** Issue #22 describes New Task as `kiln send` to
`human-in-the-loop`. Taken literally that starts nothing: the human role is an interactive
session (or, with the `inbox` pane, a display), so a message parked in its queue waits for a
person to forward it by hand — and Phase 1's stated goal is to start a task from the browser
and watch the card move. So the two intake paths are split by what they are for:

* `new_task` sends **from** the human role **to** the intake role, which is what the routing
  table says the human hands off to (`specifier` in the shipped `full` profile). Identical
  to `kiln send --to specifier --from human-in-the-loop`, which is how a human starts work
  today. This is the one that moves cards.
* `chat` sends **to** the human role's own queue — the "chat to the master agent" rail from
  the issue's Purpose section. The inbox pane surfaces it and the human's session answers it.
  It deliberately does not start a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from kiln.scheduler.domain.status_contract import PENDING_HANDOFF, is_valid_work_item_name
from kiln.scheduler.infrastructure.cli import dashboard, retry, send
from kiln.scheduler.infrastructure.persistence import db
from launcher import stop

log = logging.getLogger(__name__)

#: What `POST /api/teardown` must carry in its body to be honoured. A confirmation string
#: rather than a flag: teardown kills every Kiln process on the machine, and a request that
#: reaches this endpoint by accident (a stale tab replaying, a mis-wired fetch) must not be
#: able to satisfy it by omission.
TEARDOWN_CONFIRMATION = "TEARDOWN"


class ActionError(Exception):
    """A refused action, with a message meant for the operator's screen."""


@dataclass(frozen=True)
class ActionContext:
    """What the write endpoints need, gathered once at startup rather than per request."""

    db_path: Path
    branch: str
    human_role: str
    intake_role: str
    sessions_file: Path


def send_to(ctx: ActionContext, *, target: str, summary: str, work_item: str = "") -> dict:
    """
    Queue one handoff for a role the operator chose.

    The general form of what `new_task` and `chat` do, and the only one that can express
    "specifier, restart with CAT-3" — the move an operator wants when a spec turns out wrong
    or a role finished on a stale brief. Identical to `kiln send --to <role>`, which has
    always been able to do this from a shell.

    Three things it decides, each of which would be a silent failure if left to the caller:

    * **The target must be a role that actually reads its queue.** A message addressed to a
      passive pane or a typo inserts cleanly, reports success, and stops dead — nothing polls
      that queue and no error is raised anywhere. `config._validate_routing` refuses the same
      class of mistake at launch; this is the runtime half.
    * **The sender depends on the target.** `cockpit` when writing to the human's own queue,
      because a role must not appear to mail itself and the inbox pane prints the sender;
      the human role otherwise, because that is who is directing the work.
    * **The work-item name is validated**, since it becomes a grouping key — see
      `status_contract.is_valid_work_item_name`.
    """
    summary = summary.strip()
    if not summary:
        raise ActionError("a message needs something to say")

    target = _resolve_target(ctx, target)
    work_item = work_item.strip() or PENDING_HANDOFF
    if work_item.lower() != PENDING_HANDOFF and not is_valid_work_item_name(work_item):
        raise ActionError(
            f"{work_item!r} is not a usable work-item name. Names group a piece of work "
            "across roles, so they must start with a letter or digit and hold only letters, "
            "digits, spaces and . _ - / (80 characters at most)."
        )

    # A role must not appear to send itself mail, and the inbox pane prints the sender, so
    # the operator can tell a browser-typed note from a real inbound handoff.
    sender = "cockpit" if target == ctx.human_role else ctx.human_role
    message_id = send.send(
        db_path=ctx.db_path,
        sender=sender,
        target=target,
        summary=summary,
        branch=ctx.branch,
        handoff_name=work_item,
    )
    log.info("cockpit queued %s -> %s (id=%s)", sender, target, message_id[:8])
    return {"message_id": message_id, "target": target, "sender": sender}


def new_task(ctx: ActionContext, *, summary: str, name: str = "") -> dict:
    """
    Start a piece of work: one handoff from the human to the intake role.

    A preset over `send_to`, kept as its own entry point because the *browser* should not
    have to know which role is the intake role — that is resolved from the profile's routing
    at launch and lives here.

    `name` is optional. Leaving it as `pending` lets the specifier invent the identity, which
    is right for a loosely described request; supplying one is right when the name already
    exists (a story id from the README), because `resolve_work_item` then carries it through
    unchanged instead of renaming it.
    """
    if not ctx.intake_role:
        raise ActionError(
            "this cockpit was started without an intake role, so it does not know which "
            "role a new task goes to. Relaunch through `kiln`, or pass --intake-role."
        )
    if not summary.strip():
        raise ActionError("a task needs a description")
    return send_to(ctx, target=ctx.intake_role, summary=summary, work_item=name)


def chat(ctx: ActionContext, *, summary: str, work_item: str = "") -> dict:
    """
    Put a note in the human role's own queue.

    A preset over `send_to`. Worth knowing what it does *not* do in a profile that runs an
    `inbox` pane: that pane polls the human's queue every couple of seconds unattended and
    marks what it finds `processed`, while the human's LLM session only reads the queue while
    it happens to be blocked in `wait_for_message`. The inbox wins essentially always, so
    this reaches the operator's notification pane rather than the agent.
    """
    if not summary.strip():
        raise ActionError("an empty message says nothing")
    return send_to(ctx, target=ctx.human_role, summary=summary, work_item=work_item)


def _resolve_target(ctx: ActionContext, target: str) -> str:
    """
    The role a message may be addressed to, or ActionError naming the ones that exist.

    Checked against the launched swarm rather than a profile: the cockpit deliberately does
    not parse profiles, and `.kiln/sessions` is what it already reads everywhere else.
    """
    target = target.strip()
    if not target:
        raise ActionError("no target role given")

    sessions = dashboard.read_sessions(ctx.sessions_file)
    addressable = [session.role for session in dashboard.visible_roles(sessions)]
    if target in addressable:
        return target

    known = ", ".join(addressable) or "(none — no swarm is running here)"
    if any(session.role == target for session in sessions):
        raise ActionError(
            f"{target!r} runs no agent, so nothing would ever read the message. "
            f"Addressable roles: {known}"
        )
    raise ActionError(f"{target!r} is not a role in this swarm. Addressable roles: {known}")


def retry_message(ctx: ActionContext, *, message_id: str, guidance: str = "") -> dict:
    """
    Send one escalated message back to the role that failed on it.

    Issue #22 spells this `POST /api/retry/<role>`, but the operation the scheduler actually
    has is per **message**: `retry.resume` re-queues the same row so the work item, its lap
    count and its cost history stay attached to one identity. A role is not a unit that can
    be retried — it may hold several failed messages, and picking one for the operator would
    be a guess. Every Attention row carries the id, so the browser has it either way.
    """
    message_id = _resolve_message_id(ctx, message_id)
    row = retry.resume(db_path=ctx.db_path, message_id=message_id, guidance=guidance.strip())
    if row is None:
        raise ActionError(
            f"{message_id[:8]} is not a failed message, so there is nothing to send back."
        )
    log.info("cockpit resumed %s -> %s", message_id[:8], row["target"])
    return {"message_id": row["id"], "target": row["target"]}


def check_confirmation(confirm: str) -> None:
    """
    Refuse a teardown that did not say the word. Raises ActionError.

    Separate from `teardown` because the two happen at different moments: the request must
    be *rejected* while there is still a connection to reject it on, and then carried out
    after the reply has been written — the cockpit process is one of the things being
    stopped (see `teardown`).
    """
    if confirm != TEARDOWN_CONFIRMATION:
        raise ActionError(
            f"teardown needs confirm={TEARDOWN_CONFIRMATION!r}; nothing was stopped"
        )


def teardown(ctx: ActionContext, *, confirm: str) -> dict:
    """
    Stop every Kiln process on this machine — `kiln --stop`, from the browser.

    Machine-wide by design, matching `--stop`: process discovery matches command lines, not
    projects. The confirmation string is the guard, and the roles list comes from this
    project's sessions file only so tmux sessions get closed too.

    **This kills its own caller.** `cockpit.server` is in `stop.KILN_PROCESS_MARKERS`, as it
    must be — a cockpit surviving `kiln --stop` would keep a port bound and keep offering
    buttons for a swarm that no longer exists. So there is no return value any HTTP client
    will see, and the server calls this only after its reply has gone out.
    """
    check_confirmation(confirm)
    roles = _session_roles(ctx.sessions_file)
    pids = stop.stop_all(roles)
    log.info("cockpit teardown stopped %d process(es)", len(pids))
    return {"stopped": len(pids), "pids": pids}


def _resolve_message_id(ctx: ActionContext, prefix: str) -> str:
    """
    Accept the eight-character id the UI shows as well as the full one.

    The board and the Attention rail print short ids for the same reason `kiln retry --list`
    does — a 32-character uuid is unreadable — so the endpoint has to take what it displays.
    """
    prefix = prefix.strip()
    if not prefix:
        raise ActionError("no message id given")
    if db.get_message(ctx.db_path, prefix) is not None:
        return prefix

    matches = [
        str(row["id"]) for row in db.failed_messages(ctx.db_path, ctx.branch)
        if str(row["id"]).startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ActionError(f"no failed message starting with {prefix!r}")
    raise ActionError(f"{prefix!r} matches {len(matches)} failed messages")


def _session_roles(sessions_file: Path) -> list[str]:
    """
    Role names from `.kiln/sessions`, or none when the file is gone.

    Through `dashboard.read_sessions` rather than splitting tabs here: that function already
    owns the file's format, and a second parser is one more thing to fix when the format
    grows a column.
    """
    return [session.role for session in dashboard.read_sessions(sessions_file)]
