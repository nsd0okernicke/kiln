"""
Deterministic replacement for one auto-mode wrapper LLM session.

`run_once` performs exactly one poll/merge/delegate/handoff cycle and returns. It contains
no loop and no sleep, so tests drive it directly, repeatedly, with no real time elapsing —
`main()` is the only thing that loops. Every dependency that touches the outside world
(the worker invocation, the clock, the status writer) arrives through SchedulerContext.

The cycle mirrors constitution/workflow.md and the kiln-receive/kiln-handoff skills, which
remain the canonical prose spec and the thing to diff this behaviour against.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import db, git_ops, handoff, pane_status, status_contract, verify
from .adapters import DEFAULT_IDLE_TIMEOUT_SEC, TokenUsage, WorkerInvocation
from .routing import RoutingTable, load_routing_table, parse_routing_arguments
from .worker_prompt import WorkerDefinition, build_task_prompt, load_worker_definition

log = logging.getLogger(__name__)

# Cycle outcomes.
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

ESCALATION_TARGET = "human-in-the-loop"
DEFAULT_POLL_INTERVAL_SEC = 2.0

#: Priority for messages a human should see but need not act on -- workflow.md's "100+:
#: informational". A terminated chain is reported at this level so it lands in the inbox
#: without competing with real work for attention.
INFORMATIONAL_PRIORITY = 100

# The scheduler pane is the operator's only window into work that used to be a visible chat
# session, so each state transition gets a glyph to make the cycle scannable at a glance.
# Rendering these requires UTF-8 stdout — see enable_unicode_output().
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
ICON_START = "\N{ROCKET}"


def enable_unicode_output() -> None:
    """
    Force UTF-8 on stdout/stderr so the narration glyphs cannot crash the process.

    On Windows these default to the console codepage (cp1252 on this machine); writing an
    emoji then raises UnicodeEncodeError and takes the scheduler down mid-cycle. The
    launcher also sets PYTHONIOENCODING, but this covers running the module directly.
    `errors="replace"` means a terminal that genuinely cannot render a glyph shows a
    placeholder instead of failing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):  # exotic stream types
                reconfigure(encoding="utf-8", errors="replace")


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


@dataclass
class SchedulerContext:
    """Everything one scheduler process needs; injected so run_once stays testable."""

    role: str
    branch: str
    db_path: Path
    worktree: Path
    routing: RoutingTable
    definition: WorkerDefinition
    run_worker: Callable[..., WorkerInvocation]
    clock: Callable[[], datetime] = datetime.now
    set_status: Callable[..., None] = lambda _state, **_kwargs: None
    #: One retry, then escalate — matches loop-auto-*.md step 4.
    max_attempts: int = 2
    #: Consecutive escalations before this role stops polling entirely.
    escalation_limit: int = 3
    #: How many times one work item may reach this role before the cycle escalates instead
    #: of running. None means no ceiling — the shipped default, since an unbounded loop is
    #: only expensive, not wrong, and a badly-chosen ceiling stops real work.
    max_cycles: int | None = None
    #: Dollars this role may spend on one work item before escalating. Also handed to the
    #: worker CLI as a per-invocation ceiling, minus what has already been spent, on backends
    #: whose adapter supports the flag. None means no ceiling.
    max_budget_usd: float | None = None
    #: Runs the role's `verify` command, or None when it declares none. Injected like
    #: `run_worker` so the cycle stays testable without spawning a real test suite.
    run_verify: Callable[[], verify.VerifyResult] | None = None

    def timestamp(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SchedulerState:
    """Mutable state that must survive across cycles."""

    consecutive_escalations: int = 0
    halted: bool = False
    #: Whether the loop has already announced the halt. A parked scheduler polls forever, so
    #: without this the pane fills with the same line every poll interval.
    parked: bool = False
    #: Dollars this process has spent per work item. In memory, like
    #: `consecutive_escalations`: the queue stores no per-message cost, so persisting this
    #: would mean a schema change. The consequence is worth knowing -- a restarted scheduler
    #: starts the tally at zero, so the cap bounds one process's spend on one work item, not
    #: the work item's lifetime spend.
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
class _Attempts:
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
        """
        Usage across every attempt, retries included, kept broken down by kind.

        A retried cycle costs the sum of its attempts, not the last one -- reporting only
        the successful attempt would make the expensive cycles look like the cheap ones.
        Invocations reporting no usage (`tokens is None`) contribute nothing rather than
        being counted as zero-token successes.

        Summed field-wise via `TokenUsage.__add__` rather than collapsed to a total,
        because which *kind* of token a role burns is the actionable part -- see
        `PaneStatus.tokens`.
        """
        total = TokenUsage()
        for invocation in self.invocations:
            if invocation.tokens is not None:
                total = total + invocation.tokens
        return total


def should_retry(invocations: Sequence[WorkerInvocation], max_attempts: int) -> bool:
    """
    Pure policy: retry only while a *failed* attempt has budget left.

    Kept separate from run_once so the retry/escalate rule can be tested as a function over
    a sequence of results, with no DB, git or worker involved.
    """
    if not invocations or invocations[-1].is_done:
        return False
    return len(invocations) < max_attempts


def run_once(ctx: SchedulerContext, state: SchedulerState) -> CycleResult:
    """Perform one cycle. Returns IDLE when the inbox is empty — the caller sleeps."""
    # A halted role keeps polling, but only for a message a human explicitly sent back. It
    # has already failed three cycles in a row; taking the next ordinary handoff would fail a
    # fourth time. Waiting here rather than exiting is what makes `kiln retry` able to reach
    # it at all -- a re-queued message for a dead scheduler goes into a queue nobody reads.
    if state.halted:
        ctx.set_status("halted")
        message = db.fetch_resume(ctx.db_path, ctx.role, ctx.branch)
        if not message:
            return CycleResult(HALTED, detail=f"{ctx.role} halted; waiting for `kiln retry`")
        log.warning(
            f"{ICON_RETRY} resumed by a human; clearing %d consecutive escalation(s)",
            state.consecutive_escalations,
        )
        state.halted = False
        state.consecutive_escalations = 0
    else:
        ctx.set_status("waiting")
        message = db.fetch_and_deliver(ctx.db_path, ctx.role, ctx.branch)
        if not message:
            return CycleResult(IDLE)

    message_id = str(message["id"])
    content = str(message["content"])
    ctx.set_status("receiving")
    db.mark_processing(ctx.db_path, message_id)
    _persist_inbound(ctx, content)

    inbound = handoff.parse_handoff(content)
    if inbound.is_resume:
        log.info("human guidance attached: %s", inbound.guidance.replace("\n", " ")[:160])
    # Narrate the cycle: this pane is the operator's only window into work that used to be
    # visible as a running chat session.
    log.info(
        f"{ICON_RECEIVED} received handoff %s from %s (name=%s)",
        message_id[:8], inbound.sender or "?", inbound.handoff or "?",
    )
    target = ctx.routing.resolve(ctx.role, inbound.sender)

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

    # Merge whatever the sender pointed at -- their commit, or failing that the branch they
    # named. `already_contains` first: `merge_commit`'s `--no-ff` is what leaves an anchor for
    # `squash_anchor`, and merging something we already have produces no commit and therefore
    # no anchor.
    merge_target = inbound.merge_target
    if merge_target and not git_ops.already_contains(merge_target, ctx.worktree):
        log.info(f"{ICON_MERGE} merging %s from %s", merge_target[:8], inbound.branch or "?")
        merged = git_ops.merge_commit(
            merge_target, ctx.worktree, message=merge_commit_message(ctx.role, inbound)
        )
        if not merged.ok:
            detail = f"merge of {merge_target} failed: {merged.output}"
            log.error(detail)
            return _escalate(ctx, state, message_id, inbound, detail, MERGE_FAILED)

    anchor = git_ops.squash_anchor(ctx.worktree)
    attempts = _delegate(ctx, state, inbound)

    if not attempts.last.is_done:
        detail = f"worker blocked after {len(attempts.invocations)} attempt(s): " + (
            attempts.last.result.summary
        )
        return _escalate(
            ctx, state, message_id, inbound, detail, ESCALATED,
            cost=attempts.cost, attempts=len(attempts.invocations), tokens=attempts.tokens,
        )

    return _hand_off(ctx, state, message_id, inbound, target, anchor, attempts)


def persist_inbound(worktree: str | Path, content: str) -> Path | None:
    """
    Write tmp/handoff-in.md verbatim — /kiln-receive step 1.

    Kiln's own generated files are force-ignored first: otherwise this very file — and the
    launcher's `.claude/settings.json` alongside it — gets swept into the squash commit and
    blocks the next role's merge.

    Public because the human's inbox performs the same step; it is the file a person's own
    session reads to see what arrived. Returns the path, or None when it could not be
    written — never raises, because no cycle should fail over a debug artefact.
    """
    try:
        git_ops.ensure_generated_ignored(worktree)
        tmp_dir = Path(worktree) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "handoff-in.md"
        target.write_text(content, encoding="utf-8")
        return target
    except OSError as exc:
        log.warning("could not write tmp/handoff-in.md: %s", exc)
        return None


def _persist_inbound(ctx: SchedulerContext, content: str) -> None:
    persist_inbound(ctx.worktree, content)


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
    try:
        logs_dir = ctx.db_path.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        target = logs_dir / f"worker-debug-{ctx.role}-attempt{attempt}.log"
        target.write_text(invocation.raw_output or "(no output captured)", encoding="utf-8")
        log.info("worker output for attempt %d saved to %s", attempt, target)
    except OSError as exc:
        log.warning("could not save worker debug output: %s", exc)


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
            ctx.definition.name, len(attempts.invocations) + 1, ctx.max_attempts,
        )
        kwargs: dict[str, object] = {}
        if ctx.max_budget_usd is not None:
            # The *remaining* budget, not the whole cap: a retry after a $4 first attempt
            # under a $5 cap must not be handed $5 again. Passed only when configured, so
            # adapters without the flag are unaffected.
            # `state` already includes this cycle's earlier attempts -- record_spend runs
            # after every invocation below -- so subtracting attempts.cost as well would
            # charge the retry twice.
            remaining = ctx.max_budget_usd - state.spend_on(work_item_of(inbound.handoff))
            kwargs["max_budget_usd"] = max(remaining, 0.0)

        attempts.invocations.append(
            ctx.run_worker(prompt=prompt, attempt=len(attempts.invocations) + 1, **kwargs)
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

    squashed = git_ops.squash_since(anchor, f"{commit_prefix(ctx.role)} {summary}", ctx.worktree)
    if not squashed.ok:
        detail = f"squash failed: {squashed.output}"
        log.error(detail)
        return _escalate(
            ctx, state, message_id, inbound, detail, ESCALATED,
            cost=attempts.cost, attempts=len(attempts.invocations), tokens=attempts.tokens,
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
    db.mark_processed(ctx.db_path, message_id)

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

    arrivals = db.count_work_item_arrivals(ctx.db_path, work_item, ctx.branch, ctx.role)
    if arrivals <= ctx.max_cycles:
        return ""
    return (
        f"work item {work_item!r} has reached {ctx.role} {arrivals} times, over the "
        f"limit of {ctx.max_cycles}; stopping instead of running another cycle"
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
    if spent < ctx.max_budget_usd:
        return ""
    return (
        f"work item {work_item or '(unnamed)'!r} has cost ${spent:.2f} at {ctx.role}, "
        f"at or over the ${ctx.max_budget_usd:.2f} cap; stopping instead of spending more"
    )


def _produced_work(ctx: SchedulerContext, anchor: str) -> bool:
    """
    Did this cycle actually change anything since the anchor?

    Deliberately the *same* pair of predicates `git_ops.squash_since` uses to recognise
    "nothing to squash", so NO_OP fires in exactly the cases that branch would have caught --
    where it quietly returned the existing HEAD and let the caller hand off a commit
    containing none of its own work. Any other rule here would make the two disagree about
    what an empty cycle is.
    """
    return git_ops.has_commits_since(anchor, ctx.worktree) or git_ops.has_pending_changes(
        ctx.worktree
    )


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
            commit=git_ops.head_commit(ctx.worktree) or inbound.commit,
            summary=(
                f"Chain ended at {ctx.role}: the cycle produced no changes, so nothing was "
                f"handed off. Worker reported: {summary}"
            ),
            next_role=ESCALATION_TARGET,
            timestamp=ctx.timestamp(),
        ),
        work_item=work_item_of(inbound.handoff),
        priority=INFORMATIONAL_PRIORITY,
    )
    db.mark_processed(ctx.db_path, message_id)
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
        commit=git_ops.head_commit(ctx.worktree) or inbound.commit,
        summary="Health-check ping forwarded.",
        next_role=target,
        timestamp=ctx.timestamp(),
        ping=True,
        trail=trail,
    )
    _insert_verified(ctx, target, outbound, work_item=work_item_of(inbound.handoff))
    db.mark_processed(ctx.db_path, message_id)
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
        commit=git_ops.head_commit(ctx.worktree) or inbound.commit,
        summary=f"ESCALATION from {ctx.role}: {detail}",
        next_role=ESCALATION_TARGET,
        timestamp=ctx.timestamp(),
        escalation=True,
    )
    _insert_verified(
        ctx, ESCALATION_TARGET, outbound, work_item=work_item_of(inbound.handoff)
    )
    # `failed`, not `processed`: the escalated message stays addressable, with its reason in
    # the `error` column, so `kiln retry` can send this exact row back rather than the human
    # having to start a new work item carrying none of the failed cycle's context. It is
    # still out of `processing`, which is what marking it processed was protecting against,
    # and `fetch_and_deliver` does not select `failed`, so nothing re-serves it by accident.
    db.mark_failed(ctx.db_path, message_id, detail)

    state.consecutive_escalations += 1
    ctx.set_status("blocked")
    log.error(f"{ICON_ESCALATE} escalated to %s: %s", ESCALATION_TARGET, detail)

    if state.consecutive_escalations >= ctx.escalation_limit:
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
                commit=git_ops.head_commit(ctx.worktree) or inbound.commit,
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
    priority: int = db.DEFAULT_PRIORITY,
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
        message_id = db.insert_handoff(
            ctx.db_path, ctx.role, target, content, ctx.branch,
            priority=priority, work_item=work_item,
        )
        if db.message_exists(ctx.db_path, message_id):
            return message_id
        log.warning("handoff insert not visible after attempt %d; retrying", attempt)
    log.error("handoff to %s could not be verified after 2 attempts", target)
    return None


def make_status_writer(
    role: str,
    script: Path | None,
    worker_timeout: int | None = None,
    model: str = "",
) -> Callable[..., None]:
    """
    Status writer that shells out to set-status.py, or a no-op when it is absent.

    `cycles`/`cost_usd`/`tokens` are optional and forwarded as `--cycles=`/`--cost=`/
    `--tokens-*=` flags -- the dashboard's swarm-wide totals read these straight out of the
    JSON set-status.py writes. Omitted (not passed as 0) when the caller doesn't have them,
    matching set-status.py's own build_status(): a role that never tracks cost must not have
    its status file claim "$0.00 spent" as if that were a measured fact.
    """
    if not script or not Path(script).is_file():
        return lambda _state, **_kwargs: None

    def _write(
        state: str,
        *,
        cycles: int | None = None,
        cost_usd: float | None = None,
        tokens: TokenUsage | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        command = [sys.executable, str(script), role, state]
        if cycles is not None:
            command.append(f"--cycles={cycles}")
        if cost_usd is not None:
            command.append(f"--cost={cost_usd}")
        if attempt is not None:
            command.append(f"--attempt={attempt}")
        if max_attempts is not None:
            command.append(f"--max-attempts={max_attempts}")
        # Constant for the process, written on every status so the dashboard always has it.
        # It travels through the status file rather than being re-derived from the profile:
        # the dashboard would otherwise have to parse profiles and could disagree with what
        # the scheduler was actually launched with.
        if worker_timeout is not None:
            command.append(f"--worker-timeout={worker_timeout}")
        # Constant for the process, like the timeout above, and travelling the same way and
        # for the same reason: this is the *resolved* model (flag, else the worker
        # definition's frontmatter, else a backend default), and only this process knows it.
        # A reader that consulted the profile would show nothing for a role whose model
        # comes from frontmatter.
        if model:
            command.append(f"--model={model}")
        if tokens is not None:
            # Each kind as its own flag rather than one total: set-status.py is copied
            # verbatim into every worktree and cannot import TokenUsage to unpack a
            # structured value, so the breakdown has to survive as flat scalars.
            command += [
                f"--tokens-in={tokens.input_tokens}",
                f"--tokens-out={tokens.output_tokens}",
                f"--tokens-cache-read={tokens.cache_read_tokens}",
                f"--tokens-cache-write={tokens.cache_creation_tokens}",
            ]
        try:
            subprocess.run(
                command,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("set-status failed (non-fatal): %s", exc)

    return _write


def resolve_model(args: argparse.Namespace, definition: WorkerDefinition) -> str:
    """
    CLI flag wins, then the worker definition's own frontmatter, then a cheap default.

    The default is Claude-specific ("sonnet" is not a model name Copilot or Codex recognise),
    so it only applies when `args.agent == "claude"`. For the other backends, an empty string
    means "no `--model`/`-m` flag at all" -- each adapter already treats that as "let the CLI
    pick its own default", matching how `commands.py` already handles an unset model for
    wrapper-mode Copilot.
    """
    if args.model:
        return args.model
    if definition.model:
        return definition.model
    return "sonnet" if args.agent == "claude" else ""


def display_model(args: argparse.Namespace, definition: WorkerDefinition) -> str:
    """
    `resolve_model()`'s value, but never the empty string -- an unset model for
    copilot/codex/grok means "the CLI picks its own default", and printing that blank in a
    banner or log line reads as broken configuration rather than a deliberate choice.
    """
    return resolve_model(args, definition) or "(CLI default)"


def format_banner(ctx: SchedulerContext, args: argparse.Namespace) -> list[str]:
    """
    The pane header: what this scheduler was actually configured to do.

    A pane otherwise opens on the echoed `python -m scheduler.role_scheduler --role ...`
    command line — complete, and unreadable. Same facts, laid out for a human, including
    the resolved routing so a misrouted handoff is diagnosable before it happens.
    """
    routes = [
        f"{rule.target} (when sender is {rule.when_sender})" if rule.when_sender else rule.target
        for rule in ctx.routing.rules
        if rule.role == ctx.role
    ]
    fields = [
        ("role", ctx.role),
        ("branch", ctx.branch),
        ("worker", f"{ctx.definition.name} ({args.agent} {display_model(args, ctx.definition)})"),
        ("hands off to", ", ".join(routes) or "(no route - handoffs will escalate)"),
        ("worktree", str(ctx.worktree)),
        ("workflow", str(args.workflow)),
        ("queue", str(ctx.db_path)),
        ("poll / worker timeout", f"{args.poll_interval:g}s / {args.worker_timeout}s"),
        ("idle timeout", f"{args.worker_idle_timeout:g}s" if args.worker_idle_timeout
         else "off"),
    ]
    if args.log_file:
        fields.append(("log", str(args.log_file)))

    width = max(len(name) for name, _ in fields)
    rule = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * (width + 4 + 40)
    return [
        f"{ICON_START} Kiln scheduler",
        rule,
        *(f"  {name:<{width}}  {value}" for name, value in fields),
        rule,
    ]


def _make_worker_output_emitter() -> Callable[[str], None]:
    """
    Print streamed worker output tinted, so it reads as a distinct voice from the
    scheduler's own (unstyled) log lines sharing the same pane.

    Piped or captured output (tests, `> log.txt`) must stay free of escape sequences,
    matching pane_status.StatusBar's own rule for the same reason.
    """
    if not sys.stdout.isatty():
        return lambda line: print(line, flush=True)
    return lambda line: print(pane_status.tint_worker_output(line), flush=True)


def build_context(args: argparse.Namespace) -> SchedulerContext:
    """Assemble a context from CLI arguments."""
    from .adapters import claude_adapter, codex_adapter, copilot_adapter, grok_adapter

    adapters = {
        "claude": claude_adapter, "copilot": copilot_adapter,
        "codex": codex_adapter, "grok": grok_adapter,
    }
    adapter = adapters[args.agent]

    definition = load_worker_definition(args.worker_agent)
    model = resolve_model(args, definition)
    emit_worker_output = _make_worker_output_emitter()

    def run_worker(
        *, prompt: str, attempt: int = 1, max_budget_usd: float | None = None
    ) -> WorkerInvocation:
        debug_base = None
        if args.worker_debug:
            logs_dir = Path(args.db_path).parent / "logs"
            debug_base = logs_dir / f"agent-debug-{args.role}-attempt{attempt}"
        # Only Claude's CLI takes a budget flag today; its adapter advertises that rather
        # than this function hardcoding a backend name. Grok reports cost but has no such
        # flag, so its cap is enforced by the scheduler's own tally alone.
        budget: dict[str, object] = {}
        if max_budget_usd is not None and getattr(adapter, "SUPPORTS_BUDGET_FLAG", False):
            budget["max_budget_usd"] = max_budget_usd
        return adapter.run_worker(
            definition=definition,
            prompt=prompt,
            cwd=args.worktree,
            model=model,
            timeout=args.worker_timeout,
            # 0 means "off", not "kill immediately" -- the watchdog reads None as disabled,
            # and a bare 0 would trip on the first poll before the worker printed anything.
            idle_timeout=args.worker_idle_timeout or None,
            on_output=emit_worker_output,
            debug_base=debug_base,
            **budget,
        )

    return SchedulerContext(
        role=args.role,
        branch=args.branch,
        db_path=Path(args.db_path),
        worktree=Path(args.worktree),
        # A profile with its own routing sends it as --route arguments and replaces
        # workflow.md's table outright; without them the file stays the source.
        routing=(
            parse_routing_arguments(args.route)
            if args.route
            else load_routing_table(args.workflow)
        ),
        definition=definition,
        run_worker=run_worker,
        set_status=make_status_writer(
            args.role, args.status_script, worker_timeout=args.worker_timeout,
            # `resolve_model`, not `args.model`: the same value the worker is actually
            # invoked with, including the frontmatter and default fallbacks.
            model=model,
        ),
        max_attempts=args.max_attempts,
        escalation_limit=args.escalation_limit,
        max_cycles=args.max_cycles,
        max_budget_usd=args.max_budget_usd,
        run_verify=(
            (lambda: verify.run(args.verify, args.worktree, timeout=args.verify_timeout))
            if args.verify
            else None
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiln deterministic role scheduler.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--workflow", required=True, help="path to constitution/workflow.md")
    parser.add_argument("--worker-agent", required=True, help="generated worker agent file")
    parser.add_argument(
        "--agent", default="claude", choices=["claude", "copilot", "codex", "grok"]
    )
    parser.add_argument(
        "--route", action="append", default=[], metavar="ROLE=TARGET[:WHEN_SENDER]",
        help=(
            "one handoff routing rule from the profile, repeatable. Given at all, "
            "these replace the --workflow table entirely rather than adding to it."
        ),
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--status-script", default=None)
    parser.add_argument(
        "--log-file", default=None,
        help="also write this role's scheduler log here, so a crash leaves evidence",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument(
        "--worker-idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SEC,
        help=(
            "kill a worker that has produced no output for this long, even if its total "
            "timeout has not expired. 0 disables it. Every hang seen live went silent and "
            "stayed silent, so the total cap alone bills the full timeout for a worker that "
            f"already stopped (default: {DEFAULT_IDLE_TIMEOUT_SEC}s)"
        ),
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help=(
            "escalate instead of working once one work item has reached this role more than "
            "N times; unbounded by default"
        ),
    )
    parser.add_argument(
        "--max-attempts", type=int, default=SchedulerContext.max_attempts,
        help="worker attempts per handoff before escalating",
    )
    parser.add_argument(
        "--escalation-limit", type=int, default=SchedulerContext.escalation_limit,
        help="consecutive escalations before this role stops taking new work",
    )
    parser.add_argument(
        "--verify", default="",
        help=(
            "shell command run in this role's worktree after the worker reports done; a "
            "non-zero exit costs an attempt and its output goes into the retry"
        ),
    )
    parser.add_argument(
        "--verify-timeout", type=int, default=verify.DEFAULT_VERIFY_TIMEOUT_SEC,
        help="seconds before the verify command is killed and treated as a failure",
    )
    parser.add_argument(
        "--max-budget-usd", type=float, default=None,
        help=(
            "escalate instead of working once this role has spent N dollars on one work "
            "item; only meaningful on a backend that reports cost (claude, grok)"
        ),
    )
    parser.add_argument(
        "--worker-debug", action="store_true",
        help=(
            "write the backend CLI's own internal debug trace per attempt to "
            "<db-path-dir>/logs/agent-debug-<role>-attempt<N>[.log]"
        ),
    )
    parser.add_argument(
        "--no-status-bar", action="store_true",
        help="do not reserve the pane's bottom row for the live status bar",
    )
    return parser.parse_args(argv)


#: Consecutive crashed cycles before giving up. A transient fault (locked DB, git hiccup)
#: must not end the role, but an unfixable one should not spin forever either.
MAX_CONSECUTIVE_ERRORS = 5

#: The bar has one row; a full sentence would push out the fields that matter.
MAX_BAR_DETAIL_CHARS = 60


def attach_status_bar(ctx: SchedulerContext, args: argparse.Namespace) -> pane_status.StatusBar:
    """
    Build the pane's status bar and chain it onto the role's existing status writer.

    `ctx.set_status` already feeds `.kiln/status/<role>.json`, which drives the WezTerm
    tab-bar badges. Wrapping it means every state change reaches both surfaces from the
    single existing call site, rather than each transition having to remember two.
    """
    bar = pane_status.StatusBar(
        pane_status.PaneStatus(role=ctx.role, target=ctx.routing.resolve(ctx.role) or ""),
        enabled=False if args.no_status_bar else None,
    )

    write_status = ctx.set_status

    def set_status(state: str, **extra: object) -> None:
        # bar.status.cycles/cost_usd/tokens are already tracked (see _record_cycle) -- this
        # just threads the current totals one hop further, into the JSON file the dashboard
        # reads, rather than tracking them a second time. `extra` carries per-call facts the
        # bar does not track, like which attempt is running.
        write_status(
            state,
            cycles=bar.status.cycles,
            cost_usd=bar.status.cost_usd,
            tokens=bar.status.tokens,
            **extra,
        )
        bar.update(state=state)

    ctx.set_status = set_status
    return bar


def _sync_status_totals(
    ctx: SchedulerContext, bar: pane_status.StatusBar, result: CycleResult
) -> None:
    """
    Re-emit the status file so it carries *this* cycle's totals, not the previous one's.

    `attach_status_bar`'s wrapper reads `bar.status.cycles`/`cost_usd`/`tokens` at the moment
    it writes. The last `set_status` of a cycle ("idle", "blocked") runs inside `run_once` --
    before `_record_cycle` folds the cycle into the bar -- so `.kiln/status/<role>.json`
    trailed the pane's own bar by exactly one cycle. Two surfaces disagreeing about the same
    number is the kind of thing that quietly costs a monitoring surface its credibility, so
    it is fixed by ordering rather than documented.
    """
    if result.outcome == IDLE:
        return
    ctx.set_status(bar.status.state)


def _record_cycle(bar: pane_status.StatusBar, result: CycleResult) -> None:
    """Fold one cycle's outcome into the bar. Idle polls are not cycles."""
    if result.outcome == IDLE:
        return
    detail = " ".join(result.detail.split())
    if len(detail) > MAX_BAR_DETAIL_CHARS:
        detail = detail[: MAX_BAR_DETAIL_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    bar.update(
        cycles=bar.status.cycles + 1,
        cost_usd=bar.status.cost_usd + result.cost_usd,
        # Field-wise accumulation, so the running totals keep their input/output/cache
        # split across the whole run rather than only within one cycle.
        tokens=bar.status.tokens + result.tokens,
        target=result.target or bar.status.target,
        detail=detail,
    )


def configure_logging(log_file: str | Path | None = None, label: str = "kiln-scheduler") -> None:
    """
    Log to the pane, and to a file when one is given.

    Without a file, a scheduler that dies takes its own explanation with it — the pane
    scrollback is the only record, and it is gone as soon as the window closes.

    `label` names the emitter in every line; the inbox reuses this function and is not a
    scheduler, so a pane full of `[kiln-scheduler/...]` would misattribute its output.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(path, encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - permissions/full disk
            print(f"warning: could not open scheduler log {path}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{label}/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    enable_unicode_output()
    configure_logging(args.log_file)

    ctx = build_context(args)
    state = SchedulerState()
    bar = attach_status_bar(ctx, args)

    # The banner is written by the bar rather than logged: it is a layout, and every line
    # would otherwise carry a timestamp/level prefix that defeats the alignment. The log
    # file gets the same facts as one structured line below, so a post-mortem still knows
    # the configuration.
    bar.start(format_banner(ctx, args))
    log.info(
        "scheduler started role=%s branch=%s worker=%s model=%s worktree=%s",
        ctx.role, ctx.branch, ctx.definition.name,
        display_model(args, ctx.definition), ctx.worktree,
    )

    # The launcher normally covers this, but a scheduler started by hand — or in a project
    # scaffolded before these entries existed — would otherwise commit its own scaffolding.
    git_ops.ensure_generated_ignored(ctx.worktree)

    # The bar owns the pane's scrolling region, so it must be released on every exit path
    # — including a crash. A region that outlives the process leaves the shell prompt
    # underneath it behaving strangely, long after the scheduler is gone.
    try:
        # Inside the try, not before it: recovery touches the database, and a failure there
        # must still release the scrolling region rather than leave the pane wedged.
        recover_stale_messages(ctx)
        return _run_loop(ctx, state, args, bar)
    finally:
        bar.close()


def recover_stale_messages(ctx: SchedulerContext) -> int:
    """
    Re-serve anything this role left `processing` when it was last killed. Returns the count.

    Runs once at startup, before the poll loop. See `db.recover_stale_processing` for why a
    row in that state is stale by definition and needs no timeout.

    Logged at WARNING, individually, and loudly: recovery is not free. The killed cycle may
    have left partial work in the worktree, so the worker can redo work it already did. An
    operator who sees a handoff processed twice needs this line to explain it, and it must
    outlive the pane -- which is why it goes through the log file rather than the status bar.

    A database error here is reported and swallowed rather than raised. Recovery is a repair
    on the way in, not a precondition for running: an unusable queue -- no table yet, or the
    pre-`work_item` schema that `db.ensure_schema` deliberately does not migrate -- must
    surface through the poll loop's existing retry-and-report path, which logs it with a
    traceback and exits cleanly. Raising here would kill the role at startup instead, before
    it ever reaches that machinery.
    """
    try:
        recovered = db.recover_stale_processing(ctx.db_path, ctx.role, ctx.branch)
    except sqlite3.Error as exc:
        log.warning("could not check for messages left mid-cycle: %s", exc)
        return 0
    for row in recovered:
        log.warning(
            f"{ICON_RETRY} recovered message %s from %s (work item %s), left mid-cycle by a "
            "killed scheduler; re-serving it",
            str(row["id"])[:8], row["sender"] or "?", row["work_item"] or "-",
        )
    if recovered:
        log.warning(
            f"{ICON_BLOCKED} %d recovered message(s) will be replayed against the existing "
            "worktree: partial work from the killed cycle is still there, so this role may "
            "redo work it already did",
            len(recovered),
        )
    return len(recovered)


def _run_loop(
    ctx: SchedulerContext,
    state: SchedulerState,
    args: argparse.Namespace,
    bar: pane_status.StatusBar,
) -> int:
    consecutive_errors = 0
    while True:
        try:
            result = run_once(ctx, state)
            consecutive_errors = 0
        except KeyboardInterrupt:
            # Ctrl+C in the pane is a deliberate stop, not a crash. Exiting quietly beats
            # dumping a traceback that looks like the scheduler fell over.
            log.info("interrupted; shutting down")
            return 130
        except Exception:
            # One bad cycle must not end the role. The wrapper this replaces would have
            # kept going, and an unattended swarm that dies silently is worse than one
            # that retries noisily.
            consecutive_errors += 1
            log.exception(
                f"{ICON_BLOCKED} cycle failed (%d/%d consecutive)",
                consecutive_errors, MAX_CONSECUTIVE_ERRORS,
            )
            # Through ctx.set_status, not bar.update directly -- that's what also writes
            # .kiln/status/<role>.json, which drives the WezTerm tab-bar badge. A direct
            # bar.update only ever reached this pane's own bottom row; the badge was
            # silently left showing whatever state was last written successfully.
            ctx.set_status("blocked")
            bar.update(detail=f"cycle failed ({consecutive_errors})")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error(f"{ICON_HALT} too many consecutive failures; exiting")
                ctx.set_status("halted")
                return 1
            time.sleep(min(args.poll_interval * consecutive_errors, 30))
            continue

        _record_cycle(bar, result)
        _sync_status_totals(ctx, bar, result)
        if result.outcome != IDLE and result.outcome != HALTED:
            log.info("cycle -> %s %s", result.outcome, result.detail)
        if state.halted:
            # Park, do not exit. The circuit breaker used to return 1 here, which killed the
            # pane -- and a `kiln retry` that re-queues a message for a dead scheduler puts
            # it into a queue nobody reads. A parked scheduler keeps polling but accepts
            # nothing except an explicitly resumed message (see run_once), so it stays on
            # screen where the human is already looking, and it stays reachable.
            if not state.parked:
                state.parked = True
                log.error(
                    f"{ICON_HALT} scheduler halted; waiting for `kiln retry <message-id>`"
                )
            ctx.set_status("halted")
            if args.once:
                # A scripted single cycle must not block forever waiting on a human.
                return 1
            try:
                time.sleep(args.poll_interval)
            except KeyboardInterrupt:
                log.info("interrupted; shutting down")
                return 130
            continue
        state.parked = False
        if args.once:
            return 0
        if result.outcome == IDLE:
            try:
                time.sleep(args.poll_interval)
            except KeyboardInterrupt:
                # The poll sleep is where an idle scheduler spends nearly all its time, so
                # it is almost always where Ctrl+C lands.
                log.info("interrupted; shutting down")
                return 130




if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
