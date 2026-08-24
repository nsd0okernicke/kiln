"""Scheduler application state and results, independent of concrete infrastructure."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ...domain import handoff, policies, status_contract
from ...domain.models import (
    DEFAULT_PRIORITY,
    InboundMessage,
    TokenUsage,
    WorkerInvocation,
    WorkerRequest,
)
from ...domain.routing import RoutingTable
from ...domain.worker_prompt import WorkerDefinition, build_task_prompt
from ..ports import MessageQueue, VerificationResult, WorkerDebugSink, WorkerRunner, Worktree

log = logging.getLogger(__name__)

ESCALATION_TARGET = "human-in-the-loop"
INFORMATIONAL_PRIORITY = 100

ICON_RECEIVED = "\N{INBOX TRAY}"
ICON_MERGE = "\N{TWISTED RIGHTWARDS ARROWS}"
ICON_DELEGATE = "\N{ROBOT FACE}"
ICON_DONE = "\N{WHITE HEAVY CHECK MARK}"
ICON_SQUASH = "\N{PACKAGE}"
ICON_HANDOFF = "\N{OUTBOX TRAY}"
ICON_RETRY = "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
ICON_BLOCKED = "\N{NO ENTRY}"
ICON_ESCALATE = "\N{POLICE CARS REVOLVING LIGHT}"
ICON_HALT = "\N{OCTAGONAL SIGN}"
ICON_PING = "\N{TABLE TENNIS PADDLE AND BALL}"

# Cycle outcomes are application results, not CLI or persistence concerns.
IDLE = "idle"
HANDED_OFF = "handed_off"
PING_FORWARDED = "ping_forwarded"
ESCALATED = "escalated"
MERGE_FAILED = "merge_failed"
NO_ROUTE = "no_route"
HALTED = "halted"
NO_OP = "no_op"
MAX_CYCLES = "max_cycles"
COST_CAP = "cost_cap"


@dataclass
class SchedulerContext:
    """Dependencies and configuration required by one scheduler application instance."""

    role: str
    branch: str
    worktree: Path
    routing: RoutingTable
    definition: WorkerDefinition
    worker_runner: WorkerRunner
    queue: MessageQueue
    worktree_port: Worktree
    debug_sink: WorkerDebugSink
    queue_label: str = ""
    clock: Callable[[], datetime] = datetime.now
    set_status: Callable[..., None] = lambda _state, **_kwargs: None
    max_attempts: int = 2
    escalation_limit: int = 3
    max_cycles: int | None = None
    max_budget_usd: float | None = None
    run_verify: Callable[[], VerificationResult] | None = None

    def timestamp(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SchedulerState:
    """Mutable application state that survives across cycles in one process."""

    consecutive_escalations: int = 0
    halted: bool = False
    parked: bool = False
    spend_by_work_item: dict[str, float] = field(default_factory=dict)

    def spend_on(self, work_item: str | None) -> float:
        return self.spend_by_work_item.get(work_item or "", 0.0)

    def record_spend(self, work_item: str | None, cost: float) -> None:
        key = work_item or ""
        self.spend_by_work_item[key] = self.spend_by_work_item.get(key, 0.0) + cost


@dataclass(frozen=True)
class CycleResult:
    outcome: str
    message_id: str | None = None
    target: str | None = None
    detail: str = ""
    cost_usd: float = 0.0
    attempts: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class Attempts:
    """Worker attempts for one message, newest last."""

    invocations: list[WorkerInvocation] = field(default_factory=list)

    @property
    def last(self) -> WorkerInvocation:
        return self.invocations[-1]

    @property
    def cost(self) -> float:
        return sum(inv.cost_usd for inv in self.invocations)

    @property
    def tokens(self) -> TokenUsage:
        total = TokenUsage()
        for invocation in self.invocations:
            if invocation.tokens is not None:
                total = total + invocation.tokens
        return total


def should_retry(invocations: Sequence[WorkerInvocation], max_attempts: int) -> bool:
    """Retry only while the latest attempt failed and the attempt allowance remains."""
    return policies.should_retry(invocations, max_attempts)


def commit_prefix(role: str) -> str:
    """`coder` -> `[Coder]`, `human-in-the-loop` -> `[Human-in-the-loop]`."""
    return f"[{role[:1].upper()}{role[1:]}]"


def merge_commit_message(role: str, inbound: handoff.InboundHandoff) -> str:
    """
    A role-prefixed, handoff-specific subject/body for `git_ops.merge_commit`.

    Replaces git's default "Merge commit '<hash>' into <branch>" -- identical for every
    merge regardless of who sent what, and useless in `git log` without cross-referencing
    `messages.db` by hand. `role` is the merging role (the receiver), not the sender.
    """
    subject = (
        f"{commit_prefix(role)} Merge {inbound.handoff or 'handoff'} "
        f"from {inbound.sender or 'unknown'}"
    )
    body = (
        f"Sender: {inbound.sender or '-'}\n"
        f"Handoff: {inbound.handoff or '-'}\n"
        f"Branch: {inbound.branch or '-'}\n"
        f"Commit: {inbound.commit}"
    )
    return f"{subject}\n\n{body}"


_Attempts = Attempts


def _queue(ctx: SchedulerContext) -> MessageQueue:
    return ctx.queue


def _worktree(ctx: SchedulerContext) -> Worktree:
    return ctx.worktree_port


def run_once(ctx: SchedulerContext, state: SchedulerState) -> CycleResult:
    """Perform one cycle. Returns IDLE when the inbox is empty — the caller sleeps."""
    message, empty_result = _fetch_next_message(ctx, state)
    if empty_result is not None:
        return empty_result
    assert message is not None

    message_id = str(message["id"])
    inbound, target, early_result = _prepare_delegation(ctx, state, message_id, message)
    if early_result is not None:
        return early_result

    anchor = _worktree(ctx).squash_anchor()
    attempts = _delegate(ctx, state, inbound)

    return _finish_attempts(ctx, state, message_id, inbound, target, anchor, attempts)


def _receive_message(
    ctx: SchedulerContext, message_id: str, content: str
) -> handoff.InboundHandoff:
    ctx.set_status("receiving")
    _queue(ctx).mark_processing(message_id)
    _persist_inbound(ctx, content)
    return handoff.parse_handoff(content)


def _prepare_delegation(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    message: InboundMessage,
) -> tuple[handoff.InboundHandoff, str | None, CycleResult | None]:
    inbound = _receive_message(ctx, message_id, str(message["content"]))
    if inbound.is_resume:
        log.info("human guidance attached: %s", inbound.guidance.replace("\n", " ")[:160])
    log.info(
        f"{ICON_RECEIVED} received handoff %s from %s (name=%s)",
        message_id[:8],
        inbound.sender or "?",
        inbound.handoff or "?",
    )
    target = ctx.routing.resolve(ctx.role, inbound.sender)
    result = _pre_delegate_outcome(ctx, state, message_id, inbound, target)
    if result is None:
        result = _merge_inbound(ctx, state, message_id, inbound)
    return inbound, target, result


def _finish_attempts(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    inbound: handoff.InboundHandoff,
    target: str | None,
    anchor: str,
    attempts: _Attempts,
) -> CycleResult:
    if attempts.last.is_done:
        assert target is not None
        return _hand_off(ctx, state, message_id, inbound, target, anchor, attempts)
    detail = f"worker blocked after {len(attempts.invocations)} attempt(s): " + (
        attempts.last.result.summary
    )
    return _escalate(
        ctx,
        state,
        message_id,
        inbound,
        detail,
        ESCALATED,
        cost=attempts.cost,
        attempts=len(attempts.invocations),
        tokens=attempts.tokens,
    )


def _fetch_next_message(
    ctx: SchedulerContext, state: SchedulerState
) -> tuple[InboundMessage | None, CycleResult | None]:
    # A halted role keeps polling, but only for a message a human explicitly sent back. It
    # has already failed three cycles in a row; taking the next ordinary handoff would fail a
    # fourth time. Waiting here rather than exiting is what makes `kiln retry` able to reach
    # it at all -- a re-queued message for a dead scheduler goes into a queue nobody reads.
    if state.halted:
        ctx.set_status("halted")
        message = _queue(ctx).fetch_resume(ctx.role, ctx.branch)
        if not message:
            result = CycleResult(HALTED, detail=f"{ctx.role} halted; waiting for `kiln retry`")
            return None, result
        log.warning(
            f"{ICON_RETRY} resumed by a human; clearing %d consecutive escalation(s)",
            state.consecutive_escalations,
        )
        state.halted = False
        state.consecutive_escalations = 0
    else:
        ctx.set_status("waiting")
        message = _queue(ctx).fetch(ctx.role, ctx.branch)
        if not message:
            return None, CycleResult(IDLE)
    return message, None


def _pre_delegate_outcome(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    inbound: handoff.InboundHandoff,
    target: str | None,
) -> CycleResult | None:
    if not target:
        detail = f"no routing rule for role {ctx.role!r} with sender {inbound.sender!r}"
        log.error(detail)
        return _escalate(ctx, state, message_id, inbound, detail, NO_ROUTE)

    if inbound.is_ping:
        return _forward_ping(ctx, message_id, inbound, target)

    breach = _cycle_limit_breach(ctx, inbound)
    if breach:
        log.error(breach)
        return _escalate(ctx, state, message_id, inbound, breach, MAX_CYCLES)

    breach = _budget_breach(ctx, state, inbound)
    if breach:
        log.error(breach)
        return _escalate(ctx, state, message_id, inbound, breach, COST_CAP)
    return None


def _merge_inbound(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    inbound: handoff.InboundHandoff,
) -> CycleResult | None:
    # Merge whatever the sender pointed at -- their commit, or failing that the branch they
    # named. `already_contains` first: `merge_commit`'s `--no-ff` is what leaves an anchor for
    # `squash_anchor`, and merging something we already have produces no commit and therefore
    # no anchor.
    merge_target = inbound.merge_target
    if merge_target and not _worktree(ctx).already_contains(merge_target):
        log.info(f"{ICON_MERGE} merging %s from %s", merge_target[:8], inbound.branch or "?")
        merged = _worktree(ctx).merge(merge_target, merge_commit_message(ctx.role, inbound))
        if not merged.ok:
            detail = f"merge of {merge_target} failed: {merged.output}"
            log.error(detail)
            return _escalate(ctx, state, message_id, inbound, detail, MERGE_FAILED)
    return None


def _persist_inbound(ctx: SchedulerContext, content: str) -> None:
    _worktree(ctx).persist_inbound(content)


def _persist_worker_debug(
    ctx: SchedulerContext, invocation: WorkerInvocation, attempt: int
) -> None:
    """
    Save a blocked worker's raw output for post-mortem, in `.kiln/logs/`.

    `WorkerInvocation.raw_output` is captured in memory but the scheduler only ever logs
    `.result.summary` — a one-line detail, not the actual stream. That's fine when the
    summary is trustworthy, but it stopped being enough the moment a real failure showed
    "0 stream events seen" while the worker's own resumed transcript proved substantial
    activity happened: the summary alone gave no way to tell whether nothing was captured or
    nothing was written. Never raises — a debug artefact must not fail the cycle it exists to
    explain.
    """
    ctx.debug_sink.save(ctx.role, attempt, invocation.raw_output)


def _delegate(
    ctx: SchedulerContext, state: SchedulerState, inbound: handoff.InboundHandoff
) -> _Attempts:
    """
    Invoke the worker, retrying once with the failure report folded in.

    Spend is recorded here rather than at each call site because this is where it is
    incurred -- every outcome downstream (handoff, no-op, escalation) has already paid for
    the attempts, so recording per-outcome would mean four places to forget.
    """
    attempts = _Attempts()
    # A resumed message starts with the human's instructions already in the retry slot, so
    # the worker's first attempt sees them the way a retry's second attempt sees the previous
    # failure -- the same channel, which is why no new prompt plumbing was needed.
    retry_of: str | None = inbound.guidance or None

    while True:
        prompt = build_task_prompt(
            handoff_text=inbound.raw,
            role=ctx.role,
            branch=ctx.branch,
            worktree=ctx.worktree,
            retry_of=retry_of,
        )
        # The attempt travels to the status file, and from there to the dashboard: `working`
        # and `retrying` were distinct states already, but with no N/max an about-to-escalate
        # role looked exactly like a healthy one.
        ctx.set_status(
            "working" if not retry_of else "retrying",
            attempt=len(attempts.invocations) + 1,
            max_attempts=ctx.max_attempts,
        )
        log.info(
            f"{ICON_DELEGATE} delegating to %s (attempt %d/%d)",
            ctx.definition.name,
            len(attempts.invocations) + 1,
            ctx.max_attempts,
        )
        max_budget_usd: float | None = None
        if ctx.max_budget_usd is not None:
            # The *remaining* budget, not the whole cap: a retry after a $4 first attempt
            # under a $5 cap must not be handed $5 again. Passed only when configured, so
            # adapters without the flag are unaffected.
            # `state` already includes this cycle's earlier attempts -- record_spend runs
            # after every invocation below -- so subtracting attempts.cost as well would
            # charge the retry twice.
            remaining = ctx.max_budget_usd - state.spend_on(work_item_of(inbound.handoff))
            max_budget_usd = max(remaining, 0.0)

        attempts.invocations.append(
            ctx.worker_runner(
                WorkerRequest(
                    prompt=prompt,
                    attempt=len(attempts.invocations) + 1,
                    max_budget_usd=max_budget_usd,
                )
            )
        )
        state.record_spend(work_item_of(inbound.handoff), attempts.last.cost_usd)
        _apply_verification(ctx, attempts)
        if not attempts.last.is_done:
            _persist_worker_debug(ctx, attempts.last, len(attempts.invocations))

        if not should_retry(attempts.invocations, ctx.max_attempts):
            return attempts
        retry_of = attempts.last.result.summary
        log.warning(f"{ICON_RETRY} worker blocked, retrying once: %s", retry_of)


def _hand_off(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    inbound: handoff.InboundHandoff,
    target: str,
    anchor: str,
    attempts: _Attempts,
) -> CycleResult:
    ctx.set_status("handing-off")
    summary = attempts.last.result.summary or "completed cycle"
    log.info(f"{ICON_DONE} worker done: %s", summary)

    work_item = resolve_work_item(inbound.handoff, attempts.last.result.handoff_name)
    if work_item != inbound.handoff:
        log.info(f"{ICON_HANDOFF} work item named: %s", work_item)

    if not _produced_work(ctx, anchor):
        return _no_op(ctx, message_id, inbound, summary, attempts)

    log.info(f"{ICON_SQUASH} squashing work since %s", anchor[:8] if anchor else "(root)")

    squashed = _worktree(ctx).squash_since(anchor, f"{commit_prefix(ctx.role)} {summary}")
    if not squashed.ok:
        detail = f"squash failed: {squashed.output}"
        log.error(detail)
        return _escalate(
            ctx,
            state,
            message_id,
            inbound,
            detail,
            ESCALATED,
            cost=attempts.cost,
            attempts=len(attempts.invocations),
            tokens=attempts.tokens,
        )

    outbound = handoff.format_handoff(
        sender=ctx.role,
        handoff=work_item,
        branch=ctx.branch,
        commit=squashed.stdout,
        summary=summary,
        next_role=target,
        timestamp=ctx.timestamp(),
    )
    # The message header and the column are filled from the same value, deliberately: they
    # are the same fact, and the whole point of the column is that it can be trusted to
    # match what a human reads in the message.
    _insert_verified(ctx, target, outbound, work_item=work_item_of(work_item))
    _queue(ctx).mark_processed(message_id)

    state.consecutive_escalations = 0  # a clean cycle re-arms the circuit breaker
    ctx.set_status("idle")
    log.info(f"{ICON_HANDOFF} handed off to %s (commit %s)", target, squashed.stdout)
    return CycleResult(
        HANDED_OFF,
        message_id=message_id,
        target=target,
        detail=summary,
        cost_usd=attempts.cost,
        attempts=len(attempts.invocations),
        tokens=attempts.tokens,
    )


def resolve_work_item(inbound_name: str, reported_name: str) -> str:
    """
    The name this cycle's outbound handoff carries.

    A worker may only name the work when the inbound handoff is still the `pending`
    placeholder. That single restriction is what makes a work item an identity rather than a
    label: the role that first accepts a request chooses the name, and every role after it
    carries the same one, so grouping by it groups one piece of work.

    Why a worker can name anything at all: in scheduler mode the *scheduler* composes the
    outbound message, copying `Handoff:` from the inbound verbatim, and the worker's only
    output channel was the status sentinel. So `roles/specifier.md`'s instruction to "invent
    the handoff name, replacing the `pending` placeholder" could not be carried out, and every
    message in a project's queue ended up grouped under `pending` -- which
    `count_work_item_arrivals` and `spend_by_work_item` then counted across unrelated features.

    Falls back to the inbound name whenever there is nothing valid to replace it with, so a
    worker that ignores or fumbles the sentinel behaves exactly as before rather than failing
    a cycle whose actual work succeeded.
    """
    if not is_pending(inbound_name):
        return inbound_name
    return reported_name or inbound_name


def is_pending(name: str) -> bool:
    """True for the placeholder a human puts in an opening request."""
    return name.strip().lower() == status_contract.PENDING_HANDOFF


def work_item_of(name: str) -> str | None:
    """
    The grouping key for a handoff name, or None when it names nothing yet.

    The `pending` placeholder must never reach the column. It is not a work item, it is the
    absence of one, and storing it makes every unrelated intake share a bucket -- which is
    precisely the state the live database was found in.
    """
    return None if not name or is_pending(name) else name


def _apply_verification(ctx: SchedulerContext, attempts: _Attempts) -> None:
    """
    Run the role's verify command and, if it fails, rewrite the attempt as a failed one.

    **Folded into `_delegate`'s loop rather than sitting between `_delegate` and `_hand_off`.**
    That loop already owns the attempt counter, the retry prompt and `should_retry`; a verify
    step outside it would be a second retry mechanism that has to agree with `max_attempts` --
    exactly the duplication `should_retry` was extracted to prevent. Demoting the invocation
    instead means one rule governs "the worker said it was blocked" and "the worker said it
    was done but the tests disagree", and escalation counting stays in one place.

    Cost and tokens are preserved: that work was really performed and really billed, whatever
    the gate then concluded about it. Only the verdict changes.
    """
    if ctx.run_verify is None or not attempts.last.is_done:
        return

    ctx.set_status("verifying")
    result = ctx.run_verify()
    if result.ok:
        log.info(f"{ICON_DONE} %s", result.summary)
        return

    log.warning(f"{ICON_BLOCKED} %s", result.summary)
    failed = attempts.last
    attempts.invocations[-1] = replace(
        failed,
        result=status_contract.WorkerResult(
            status=status_contract.STATUS_BLOCKED,
            summary=f"{result.summary}\n\n{result.output}",
            # The worker did report; the gate overruled it. Recording otherwise would send
            # someone hunting for a missing sentinel that was there all along.
            sentinel_found=failed.result.sentinel_found,
        ),
    )


def _cycle_limit_breach(ctx: SchedulerContext, inbound: handoff.InboundHandoff) -> str:
    """
    The reason this work item has gone round too many times, or "" when it has not.

    Checked before delegating, so a swarm that has lost the plot stops *before* paying for
    another worker run rather than after. Escalating rather than hard-stopping is deliberate:
    a hard stop leaves the human a dead swarm and nothing addressable, while an escalation
    puts the reason in the inbox attached to the work item it is about.

    A work item with no name is not counted, and the `pending` placeholder counts as no name.
    Only the intake hop is legitimately unnamed, so there is nothing there to loop on -- while
    counting the placeholder would pool every unrelated feature's intake into one bucket and
    trip this guard on a swarm that is not looping at all.
    """
    work_item = work_item_of(inbound.handoff)
    if ctx.max_cycles is None or not work_item:
        return ""

    arrivals = _queue(ctx).count_arrivals(work_item, ctx.branch, ctx.role)
    return policies.cycle_limit_breach(
        arrivals=arrivals,
        max_cycles=ctx.max_cycles,
        work_item=work_item,
        role=ctx.role,
    )


def _budget_breach(
    ctx: SchedulerContext, state: SchedulerState, inbound: handoff.InboundHandoff
) -> str:
    """
    The reason this work item has cost too much, or "" when it has not.

    Checked before delegating, like the cycle limit, so the run that would breach the cap
    never starts. Escalates rather than hard-stopping, for the same reason.

    **This is only as good as the backend's cost reporting.** Copilot and Codex always report
    $0.00, so the tally never moves and the cap never fires -- which is why `parse_role`
    refuses `maxBudgetUsd` on those agents outright rather than leaving a guard that appears
    to be enforcing.
    """
    if ctx.max_budget_usd is None:
        return ""

    work_item = work_item_of(inbound.handoff)
    spent = state.spend_on(work_item)
    reason = policies.budget_breach(spent=spent, maximum=ctx.max_budget_usd, work_item=work_item)
    return reason.replace("at this role", f"at {ctx.role}")


def _produced_work(ctx: SchedulerContext, anchor: str) -> bool:
    """
    Did this cycle actually change anything since the anchor?

    Deliberately the *same* pair of predicates `git_ops.squash_since` uses to recognise
    "nothing to squash", so NO_OP fires in exactly the cases that branch would have caught --
    where it quietly returned the existing HEAD and let the caller hand off a commit
    containing none of its own work. Any other rule here would make the two disagree about
    what an empty cycle is.
    """
    return _worktree(ctx).has_commits_since(anchor) or _worktree(ctx).has_pending_changes()


def _no_op(
    ctx: SchedulerContext,
    message_id: str,
    inbound: handoff.InboundHandoff,
    summary: str,
    attempts: _Attempts,
) -> CycleResult:
    """
    End the chain: the worker finished and produced nothing, so there is nothing to forward.

    `roles/architect.md` has always said "do not hand off changes if the handoff contains no
    changes", and the scheduler did not honour it -- `squash_since` treats an empty range as
    success and returns the current HEAD, so the role forwarded a commit containing none of
    its own work and the swarm kept spending on a cycle that had already concluded.

    **The chain stopping is the point, but a silent stop is indistinguishable from a crash.**
    So one informational message goes to the human (priority 100+, not an escalation): the run
    ended correctly, it just ended. Nothing is queued for the routed target, which is what
    actually breaks the loop.

    The circuit breaker is not touched, matching `_forward_ping`: this is neither a failure to
    count nor a productive cycle to re-arm on.
    """
    log.info(
        f"{ICON_HALT} nothing to hand off -- %s produced no changes; chain ends here", ctx.role
    )
    _insert_verified(
        ctx,
        ESCALATION_TARGET,
        handoff.format_handoff(
            sender=ctx.role,
            handoff=inbound.handoff,
            branch=ctx.branch,
            commit=_worktree(ctx).head_commit() or inbound.commit,
            summary=(
                f"{ctx.role.capitalize()} reviewed the inbound handoff and produced no "
                f"additional changes. The chain ended without creating another role "
                f"handoff. Worker reported: {summary}"
            ),
            next_role=ESCALATION_TARGET,
            timestamp=ctx.timestamp(),
        ),
        work_item=work_item_of(inbound.handoff),
        priority=INFORMATIONAL_PRIORITY,
    )
    _queue(ctx).mark_processed(message_id)
    ctx.set_status("idle")
    return CycleResult(
        NO_OP,
        message_id=message_id,
        target=ESCALATION_TARGET,
        detail=summary,
        cost_usd=attempts.cost,
        attempts=len(attempts.invocations),
        tokens=attempts.tokens,
    )


def _forward_ping(
    ctx: SchedulerContext,
    message_id: str,
    inbound: handoff.InboundHandoff,
    target: str,
) -> CycleResult:
    """
    Append this role's hop to a health-check trail and forward it.

    No merge and no worker: kiln-receive/SKILL.md step 3 is explicit that a ping must not
    run the role's normal process, so the cycle stays side-effect free apart from the trail.
    """
    log.info(f"{ICON_PING} health-check ping; forwarding without delegating")
    trail = handoff.append_trail_entry(inbound.trail, ctx.role, ctx.branch)
    outbound = handoff.format_handoff(
        sender=ctx.role,
        handoff=inbound.handoff,
        branch=ctx.branch,
        commit=_worktree(ctx).head_commit() or inbound.commit,
        summary="Health-check ping forwarded.",
        next_role=target,
        timestamp=ctx.timestamp(),
        ping=True,
        trail=trail,
    )
    _insert_verified(ctx, target, outbound, work_item=work_item_of(inbound.handoff))
    _queue(ctx).mark_processed(message_id)
    ctx.set_status("idle")
    return CycleResult(PING_FORWARDED, message_id=message_id, target=target)


def _escalate(
    ctx: SchedulerContext,
    state: SchedulerState,
    message_id: str,
    inbound: handoff.InboundHandoff,
    detail: str,
    outcome: str,
    cost: float = 0.0,
    attempts: int = 0,
    tokens: TokenUsage | None = None,
) -> CycleResult:
    """
    Route a failed cycle to a human instead of forwarding it as if it succeeded.

    The inbound message is still marked processed so nothing wedges in `processing`
    forever, and the circuit breaker trips after enough consecutive failures because no
    supervisor exists to notice a scheduler looping on the same error unattended.
    """
    outbound = handoff.format_handoff(
        sender=ctx.role,
        handoff=inbound.handoff,
        branch=ctx.branch,
        commit=_worktree(ctx).head_commit() or inbound.commit,
        summary=f"ESCALATION from {ctx.role}: {detail}",
        next_role=ESCALATION_TARGET,
        timestamp=ctx.timestamp(),
        escalation=True,
    )
    _insert_verified(ctx, ESCALATION_TARGET, outbound, work_item=work_item_of(inbound.handoff))
    # `failed`, not `processed`: the escalated message stays addressable, with its reason in
    # the `error` column, so `kiln retry` can send this exact row back rather than the human
    # having to start a new work item carrying none of the failed cycle's context. It is
    # still out of `processing`, which is what marking it processed was protecting against,
    # and `fetch_and_deliver` does not select `failed`, so nothing re-serves it by accident.
    _queue(ctx).mark_failed(message_id, detail)

    state.consecutive_escalations += 1
    ctx.set_status("blocked")
    log.error(f"{ICON_ESCALATE} escalated to %s: %s", ESCALATION_TARGET, detail)

    if policies.escalation_halts(state.consecutive_escalations, ctx.escalation_limit):
        state.halted = True
        halt_detail = (
            f"{ctx.role} halted after {state.consecutive_escalations} consecutive escalations"
        )
        log.error(f"{ICON_HALT} CIRCUIT BREAKER: %s", halt_detail)
        _insert_verified(
            ctx,
            ESCALATION_TARGET,
            handoff.format_handoff(
                sender=ctx.role,
                handoff=inbound.handoff,
                branch=ctx.branch,
                commit=_worktree(ctx).head_commit() or inbound.commit,
                summary=f"CIRCUIT BREAKER: {halt_detail}. This role has stopped polling.",
                next_role=ESCALATION_TARGET,
                timestamp=ctx.timestamp(),
                escalation=True,
            ),
            work_item=work_item_of(inbound.handoff),
        )

    return CycleResult(
        outcome,
        message_id=message_id,
        target=ESCALATION_TARGET,
        detail=detail,
        cost_usd=cost,
        attempts=attempts,
        tokens=tokens or TokenUsage(),
    )


def _insert_verified(
    ctx: SchedulerContext,
    target: str,
    content: str,
    work_item: str | None = None,
    priority: int = DEFAULT_PRIORITY,
) -> str | None:
    """
    Insert a message and confirm it landed, retrying once.

    Mirrors kiln-handoff/SKILL.md steps 4-5, which exist because the INSERT has been
    observed to fail silently.

    Confirmed **by id**, not by "is there a queued message from me". The receiving scheduler
    polls every couple of seconds and can take the message one second after it is written, so
    a status-based check reports "not there" for a message that arrived perfectly. The retry
    then inserts a second copy of the same handoff. Observed live on the skill's version of
    this step -- see `db.message_exists`.
    """
    for attempt in (1, 2):
        message_id = _queue(ctx).insert(
            ctx.role,
            target,
            content,
            ctx.branch,
            priority=priority,
            work_item=work_item,
        )
        if _queue(ctx).exists(message_id):
            return message_id
        log.warning("handoff insert not visible after attempt %d; retrying", attempt)
    log.error("handoff to %s could not be verified after 2 attempts", target)
    return None
