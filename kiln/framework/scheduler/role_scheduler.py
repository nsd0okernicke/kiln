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

from . import db, git_ops, handoff, pane_status
from .adapters import WorkerInvocation
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


def merge_commit_message(role: str, inbound: handoff.InboundHandoff) -> str:
    """
    A role-prefixed, handoff-specific subject/body for `git_ops.merge_commit`.

    Replaces git's default "Merge commit '<hash>' into <branch>" -- identical for every
    merge regardless of who sent what, and useless in `git log` without cross-referencing
    `messages.db` by hand. `role` is the merging role (the receiver), not the sender.
    """
    subject = f"{commit_prefix(role)} Merge {inbound.handoff or 'handoff'} from {inbound.sender or 'unknown'}"
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
        merged = git_ops.merge_commit(
            inbound.commit, ctx.worktree, message=merge_commit_message(ctx.role, inbound)
        )
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


def _persist_worker_debug(ctx: SchedulerContext, invocation: WorkerInvocation, attempt: int) -> None:
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
        attempts.invocations.append(
            ctx.run_worker(prompt=prompt, attempt=len(attempts.invocations) + 1)
        )
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


def make_status_writer(
    role: str, script: Path | None
) -> Callable[..., None]:
    """
    Status writer that shells out to set-status.py, or a no-op when it is absent.

    `cycles`/`cost_usd` are optional and forwarded as `--cycles=`/`--cost=` flags -- the
    dashboard's swarm-wide totals read these straight out of the JSON set-status.py writes.
    Omitted (not passed as 0) when the caller doesn't have them, matching set-status.py's own
    build_status(): a role that never tracks cost must not have its status file claim
    "$0.00 spent" as if that were a measured fact.
    """
    if not script or not Path(script).is_file():
        return lambda _state, **_kwargs: None

    def _write(state: str, *, cycles: int | None = None, cost_usd: float | None = None) -> None:
        command = [sys.executable, str(script), role, state]
        if cycles is not None:
            command.append(f"--cycles={cycles}")
        if cost_usd is not None:
            command.append(f"--cost={cost_usd}")
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

    def run_worker(*, prompt: str, attempt: int = 1) -> WorkerInvocation:
        debug_base = None
        if args.worker_debug:
            logs_dir = Path(args.db_path).parent / "logs"
            debug_base = logs_dir / f"agent-debug-{args.role}-attempt{attempt}"
        return adapter.run_worker(
            definition=definition,
            prompt=prompt,
            cwd=args.worktree,
            model=model,
            timeout=args.worker_timeout,
            on_output=emit_worker_output,
            debug_base=debug_base,
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
    parser.add_argument(
        "--agent", default="claude", choices=["claude", "copilot", "codex", "grok"]
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--status-script", default=None)
    parser.add_argument(
        "--log-file", default=None,
        help="also write this role's scheduler log here, so a crash leaves evidence",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
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

    def set_status(state: str) -> None:
        # bar.status.cycles/cost_usd are already tracked (see _record_cycle) -- this just
        # threads the current totals one hop further, into the JSON file the dashboard reads,
        # rather than tracking them a second time.
        write_status(state, cycles=bar.status.cycles, cost_usd=bar.status.cost_usd)
        bar.update(state=state)

    ctx.set_status = set_status
    return bar


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
        return _run_loop(ctx, state, args, bar)
    finally:
        bar.close()


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
        if result.outcome != IDLE:
            log.info("cycle -> %s %s", result.outcome, result.detail)
        if state.halted:
            log.error(f"{ICON_HALT} scheduler halted; exiting")
            ctx.set_status("halted")
            return 1
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
