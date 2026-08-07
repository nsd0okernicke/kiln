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
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db, git_ops, handoff
from .adapters.claude_adapter import WorkerInvocation
from .routing import RoutingTable, load_routing_table
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

ESCALATION_TARGET = "human-in-the-loop"
DEFAULT_POLL_INTERVAL_SEC = 2.0

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
    set_status: Callable[[str], None] = lambda _state: None
    #: One retry, then escalate — matches loop-auto-*.md step 4.
    max_attempts: int = 2
    #: Consecutive escalations before this role stops polling entirely.
    escalation_limit: int = 3

    def timestamp(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SchedulerState:
    """Mutable state that must survive across cycles."""

    consecutive_escalations: int = 0
    halted: bool = False


@dataclass(frozen=True)
class CycleResult:
    outcome: str
    message_id: str | None = None
    target: str | None = None
    detail: str = ""
    cost_usd: float = 0.0
    attempts: int = 0


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
    if state.halted:
        return CycleResult(HALTED, detail=f"{ctx.role} halted after repeated escalations")

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

    if inbound.is_mergeable:
        log.info(f"{ICON_MERGE} merging %s from %s", inbound.commit[:8], inbound.branch or "?")
        merged = git_ops.merge_commit(inbound.commit, ctx.worktree)
        if not merged.ok:
            detail = f"merge of {inbound.commit} failed: {merged.output}"
            log.error(detail)
            return _escalate(ctx, state, message_id, inbound, detail, MERGE_FAILED)

    anchor = git_ops.squash_anchor(ctx.worktree)
    attempts = _delegate(ctx, inbound)

    if not attempts.last.is_done:
        detail = f"worker blocked after {len(attempts.invocations)} attempt(s): " + (
            attempts.last.result.summary
        )
        return _escalate(
            ctx, state, message_id, inbound, detail, ESCALATED,
            cost=attempts.cost, attempts=len(attempts.invocations),
        )

    return _hand_off(ctx, state, message_id, inbound, target, anchor, attempts)


def _persist_inbound(ctx: SchedulerContext, content: str) -> None:
    """
    Write tmp/handoff-in.md verbatim, for parity with /kiln-receive and debuggability.

    `tmp/` is force-ignored first: otherwise this very file gets swept into the squash
    commit and blocks the next cycle's merge.
    """
    try:
        git_ops.ensure_ignored("tmp/", ctx.worktree)
        tmp_dir = Path(ctx.worktree) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "handoff-in.md").write_text(content, encoding="utf-8")
    except OSError as exc:  # never fail a cycle over a debug artefact
        log.warning("could not write tmp/handoff-in.md: %s", exc)


def _delegate(ctx: SchedulerContext, inbound: handoff.InboundHandoff) -> _Attempts:
    """Invoke the worker, retrying once with the failure report folded in."""
    attempts = _Attempts()
    retry_of: str | None = None

    while True:
        prompt = build_task_prompt(
            handoff_text=inbound.raw,
            role=ctx.role,
            branch=ctx.branch,
            worktree=ctx.worktree,
            retry_of=retry_of,
        )
        ctx.set_status("working" if not retry_of else "retrying")
        log.info(
            f"{ICON_DELEGATE} delegating to %s (attempt %d/%d)",
            ctx.definition.name, len(attempts.invocations) + 1, ctx.max_attempts,
        )
        attempts.invocations.append(ctx.run_worker(prompt=prompt))

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
    log.info(f"{ICON_SQUASH} squashing work since %s", anchor[:8] if anchor else "(root)")

    squashed = git_ops.squash_since(anchor, f"{commit_prefix(ctx.role)} {summary}", ctx.worktree)
    if not squashed.ok:
        detail = f"squash failed: {squashed.output}"
        log.error(detail)
        return _escalate(
            ctx, state, message_id, inbound, detail, ESCALATED,
            cost=attempts.cost, attempts=len(attempts.invocations),
        )

    outbound = handoff.format_handoff(
        sender=ctx.role,
        handoff=inbound.handoff,
        branch=ctx.branch,
        commit=squashed.stdout,
        summary=summary,
        next_role=target,
        timestamp=ctx.timestamp(),
    )
    _insert_verified(ctx, target, outbound)
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
    _insert_verified(ctx, target, outbound)
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
    _insert_verified(ctx, ESCALATION_TARGET, outbound)
    db.mark_processed(ctx.db_path, message_id)

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
        )

    return CycleResult(
        outcome,
        message_id=message_id,
        target=ESCALATION_TARGET,
        detail=detail,
        cost_usd=cost,
        attempts=attempts,
    )


def _insert_verified(ctx: SchedulerContext, target: str, content: str) -> str | None:
    """
    Insert a message and confirm it landed, retrying once.

    Mirrors kiln-handoff/SKILL.md steps 4-5, which exist because the INSERT has been
    observed to fail silently.
    """
    for attempt in (1, 2):
        message_id = db.insert_handoff(ctx.db_path, ctx.role, target, content, ctx.branch)
        if db.verify_queued(ctx.db_path, ctx.role, ctx.branch):
            return message_id
        log.warning("handoff insert not visible after attempt %d; retrying", attempt)
    log.error("handoff to %s could not be verified after 2 attempts", target)
    return None


def make_status_writer(role: str, script: Path | None) -> Callable[[str], None]:
    """Status writer that shells out to set-status.py, or a no-op when it is absent."""
    if not script or not Path(script).is_file():
        return lambda _state: None

    def _write(state: str) -> None:
        try:
            subprocess.run(
                [sys.executable, str(script), role, state],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("set-status failed (non-fatal): %s", exc)

    return _write


def build_context(args: argparse.Namespace) -> SchedulerContext:
    """Assemble a context from CLI arguments."""
    from .adapters import claude_adapter

    definition = load_worker_definition(args.worker_agent)
    model = args.model or definition.model or "sonnet"

    def run_worker(*, prompt: str) -> WorkerInvocation:
        return claude_adapter.run_worker(
            definition=definition,
            prompt=prompt,
            cwd=args.worktree,
            model=model,
            timeout=args.worker_timeout,
        )

    return SchedulerContext(
        role=args.role,
        branch=args.branch,
        db_path=Path(args.db_path),
        worktree=Path(args.worktree),
        routing=load_routing_table(args.workflow),
        definition=definition,
        run_worker=run_worker,
        set_status=make_status_writer(args.role, args.status_script),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiln deterministic role scheduler.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--workflow", required=True, help="path to constitution/workflow.md")
    parser.add_argument("--worker-agent", required=True, help="generated worker agent file")
    parser.add_argument("--agent", default="claude", choices=["claude"])
    parser.add_argument("--model", default="")
    parser.add_argument("--status-script", default=None)
    parser.add_argument(
        "--log-file", default=None,
        help="also write this role's scheduler log here, so a crash leaves evidence",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    return parser.parse_args(argv)


#: Consecutive crashed cycles before giving up. A transient fault (locked DB, git hiccup)
#: must not end the role, but an unfixable one should not spin forever either.
MAX_CONSECUTIVE_ERRORS = 5


def configure_logging(log_file: str | Path | None = None) -> None:
    """
    Log to the pane, and to a file when one is given.

    Without a file, a scheduler that dies takes its own explanation with it — the pane
    scrollback is the only record, and it is gone as soon as the window closes.
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
        format="%(asctime)s [kiln-scheduler/%(levelname)s] %(message)s",
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
    log.info(f"{ICON_START} scheduler started for role=%s branch=%s", ctx.role, ctx.branch)

    consecutive_errors = 0
    while True:
        try:
            result = run_once(ctx, state)
            consecutive_errors = 0
        except Exception:
            # One bad cycle must not end the role. The wrapper this replaces would have
            # kept going, and an unattended swarm that dies silently is worse than one
            # that retries noisily.
            consecutive_errors += 1
            log.exception(
                f"{ICON_BLOCKED} cycle failed (%d/%d consecutive)",
                consecutive_errors, MAX_CONSECUTIVE_ERRORS,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error(f"{ICON_HALT} too many consecutive failures; exiting")
                return 1
            time.sleep(min(args.poll_interval * consecutive_errors, 30))
            continue

        if result.outcome != IDLE:
            log.info("cycle -> %s %s", result.outcome, result.detail)
        if state.halted:
            log.error(f"{ICON_HALT} scheduler halted; exiting")
            return 1
        if args.once:
            return 0
        if result.outcome == IDLE:
            time.sleep(args.poll_interval)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
