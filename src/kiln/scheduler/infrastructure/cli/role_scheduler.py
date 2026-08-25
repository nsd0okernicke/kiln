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
import logging
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ...application import process_next_message as scheduler_application
from ...application.process_next_message import (
    CycleResult,
    SchedulerContext,
    SchedulerState,
)
from ...application.recover_interrupted_work import recover_interrupted_work
from ...domain.models import TokenUsage, WorkerInvocation, WorkerRequest
from ...domain.routing import load_routing_table, parse_routing_arguments
from ...domain.worker_prompt import WorkerDefinition, load_worker_definition
from ..agents import DEFAULT_IDLE_TIMEOUT_SEC
from ..diagnostics import FileWorkerDebugSink
from ..diagnostics import verification as verify
from ..persistence import SQLiteMessageQueue
from ..runtime import configure_logging, enable_unicode_output
from ..terminal import pane_status
from ..vcs import GitWorktree

log = logging.getLogger(__name__)

# Compatibility exports for callers that historically imported application outcomes here.
IDLE = scheduler_application.IDLE
HANDED_OFF = scheduler_application.HANDED_OFF
PING_FORWARDED = scheduler_application.PING_FORWARDED
ESCALATED = scheduler_application.ESCALATED
MERGE_FAILED = scheduler_application.MERGE_FAILED
NO_ROUTE = scheduler_application.NO_ROUTE
HALTED = scheduler_application.HALTED
NO_OP = scheduler_application.NO_OP
MAX_CYCLES = scheduler_application.MAX_CYCLES
COST_CAP = scheduler_application.COST_CAP
should_retry = scheduler_application.should_retry

ESCALATION_TARGET = "human-in-the-loop"
DEFAULT_POLL_INTERVAL_SEC = 2.0

#: Workflow priority for informational messages.
INFORMATIONAL_PRIORITY = 100

# State glyphs require UTF-8 output; see `enable_unicode_output`.
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


commit_prefix = scheduler_application.commit_prefix
merge_commit_message = scheduler_application.merge_commit_message
_Attempts = scheduler_application.Attempts
_queue = scheduler_application._queue
_worktree = scheduler_application._worktree
run_once = scheduler_application.run_once
_persist_inbound = scheduler_application._persist_inbound
_persist_worker_debug = scheduler_application._persist_worker_debug
_delegate = scheduler_application._delegate
_hand_off = scheduler_application._hand_off
resolve_work_item = scheduler_application.resolve_work_item
is_pending = scheduler_application.is_pending
work_item_of = scheduler_application.work_item_of
_apply_verification = scheduler_application._apply_verification
_cycle_limit_breach = scheduler_application._cycle_limit_breach
_budget_breach = scheduler_application._budget_breach
_produced_work = scheduler_application._produced_work
_no_op = scheduler_application._no_op
_forward_ping = scheduler_application._forward_ping
_escalate = scheduler_application._escalate
_insert_verified = scheduler_application._insert_verified


def persist_inbound(worktree: str | Path, content: str) -> Path | None:
    """Compatibility wrapper for the human inbox's path-based persistence API."""
    return GitWorktree(worktree).persist_inbound(content)


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
        if worker_timeout is not None:
            command.append(f"--worker-timeout={worker_timeout}")
        if model:
            command.append(f"--model={model}")
        if tokens is not None:
            # The standalone status script accepts the usage breakdown as scalar flags.
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

    A pane otherwise opens on the echoed
    `python -m kiln.scheduler.infrastructure.cli.role_scheduler --role ...`
    command line — complete, and unreadable. Same facts, laid out for a human, including
    the resolved routing so a misrouted handoff is diagnosable before it happens.
    """
    fields = _banner_fields(ctx, args)
    width = max(len(name) for name, _ in fields)
    rule = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * (width + 4 + 40)
    return [
        f"{ICON_START} Kiln scheduler",
        rule,
        *(f"  {name:<{width}}  {value}" for name, value in fields),
        rule,
    ]


def _banner_fields(ctx: SchedulerContext, args: argparse.Namespace) -> list[tuple[str, str]]:
    fields = [
        ("role", ctx.role),
        ("branch", ctx.branch),
        ("worker", f"{ctx.definition.name} ({args.agent} {display_model(args, ctx.definition)})"),
        ("hands off to", _route_summary(ctx)),
        ("worktree", str(ctx.worktree)),
        ("workflow", str(args.workflow)),
        ("queue", ctx.queue_label),
        ("poll / worker timeout", f"{args.poll_interval:g}s / {args.worker_timeout}s"),
        ("idle timeout", f"{args.worker_idle_timeout:g}s" if args.worker_idle_timeout else "off"),
    ]
    if args.log_file:
        fields.append(("log", str(args.log_file)))
    return fields


def _route_summary(ctx: SchedulerContext) -> str:
    routes = [
        f"{rule.target} (when sender is {rule.when_sender})" if rule.when_sender else rule.target
        for rule in ctx.routing.rules
        if rule.role == ctx.role
    ]
    return ", ".join(routes) or "(no route - handoffs will escalate)"


WORKER_LOG_MAX_BYTES = 4 * 1024 * 1024


def _make_worker_output_emitter(
    log_file: str | Path | None = None,
) -> Callable[[str], None]:
    """
    Print streamed worker output tinted, so it reads as a distinct voice from the
    scheduler's own (unstyled) log lines sharing the same pane.

    Piped or captured output (tests, `> log.txt`) must stay free of escape sequences,
    matching pane_status.StatusBar's own rule for the same reason.
    """
    path = Path(log_file) if log_file else None
    log_failed = False

    def emit(line: str) -> None:
        nonlocal log_failed
        rendered = pane_status.tint_worker_output(line) if sys.stdout.isatty() else line
        print(rendered, flush=True)
        if path is None or log_failed:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (line + "\n").encode("utf-8")
            if path.is_file() and path.stat().st_size + len(encoded) > WORKER_LOG_MAX_BYTES:
                rollover = path.with_suffix(path.suffix + ".1")
                if rollover.exists():
                    rollover.unlink()
                path.replace(rollover)
            with path.open("ab") as handle:
                handle.write(encoded)
        except OSError as exc:
            log_failed = True
            log.warning("could not write worker log %s; capture disabled: %s", path, exc)

    return emit


def build_context(args: argparse.Namespace) -> SchedulerContext:
    """Assemble a context from CLI arguments."""
    from ..agents import (
        claude_adapter,
        codex_adapter,
        copilot_adapter,
        grok_adapter,
    )

    adapters = {
        "claude": claude_adapter,
        "copilot": copilot_adapter,
        "codex": codex_adapter,
        "grok": grok_adapter,
    }
    adapter = adapters[args.agent]

    definition = load_worker_definition(args.worker_agent)
    model = resolve_model(args, definition)
    emit_worker_output = _make_worker_output_emitter(
        Path(args.db_path).parent / "logs" / f"worker-{args.role}.log"
    )

    def run_worker(request: WorkerRequest) -> WorkerInvocation:
        debug_base = None
        if args.worker_debug:
            logs_dir = Path(args.db_path).parent / "logs"
            debug_base = logs_dir / f"agent-debug-{args.role}-attempt{request.attempt}"
        budget: dict[str, object] = {}
        if request.max_budget_usd is not None and getattr(adapter, "SUPPORTS_BUDGET_FLAG", False):
            budget["max_budget_usd"] = request.max_budget_usd
        return adapter.run_worker(
            definition=definition,
            prompt=request.prompt,
            cwd=args.worktree,
            model=model,
            timeout=args.worker_timeout,
            # The watchdog uses None to disable idle detection.
            idle_timeout=args.worker_idle_timeout or None,
            on_output=emit_worker_output,
            debug_base=debug_base,
            **budget,
        )

    return SchedulerContext(
        role=args.role,
        branch=args.branch,
        worktree=Path(args.worktree),
        routing=(
            parse_routing_arguments(args.route) if args.route else load_routing_table(args.workflow)
        ),
        definition=definition,
        worker_runner=run_worker,
        queue=SQLiteMessageQueue(args.db_path),
        worktree_port=GitWorktree(args.worktree),
        debug_sink=FileWorkerDebugSink(Path(args.db_path).parent / "logs"),
        queue_label=str(args.db_path),
        set_status=make_status_writer(
            args.role,
            args.status_script,
            worker_timeout=args.worker_timeout,
            model=model,
        ),
        max_attempts=args.max_attempts,
        escalation_limit=args.escalation_limit,
        max_cycles=args.max_cycles,
        max_budget_usd=args.max_budget_usd,
        run_verify=(
            (
                lambda: verify.run(
                    args.verify,
                    args.worktree,
                    timeout=args.verify_timeout,
                    project_root=Path(args.db_path).parent.parent,
                )
            )
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
    parser.add_argument("--agent", default="claude", choices=["claude", "copilot", "codex", "grok"])
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="ROLE=TARGET[:WHEN_SENDER]",
        help=(
            "one handoff routing rule from the profile, repeatable. Given at all, "
            "these replace the --workflow table entirely rather than adding to it."
        ),
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--status-script", default=None)
    parser.add_argument(
        "--log-file",
        default=None,
        help="also write this role's scheduler log here, so a crash leaves evidence",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument(
        "--worker-idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_SEC,
        help=(
            "kill a worker that has produced no output for this long, even if its total "
            "timeout has not expired. 0 disables it. Every hang seen live went silent and "
            "stayed silent, so the total cap alone bills the full timeout for a worker that "
            f"already stopped (default: {DEFAULT_IDLE_TIMEOUT_SEC}s)"
        ),
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help=(
            "escalate instead of working once one work item has reached this role more than "
            "N times; unbounded by default"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=SchedulerContext.max_attempts,
        help="worker attempts per handoff before escalating",
    )
    parser.add_argument(
        "--escalation-limit",
        type=int,
        default=SchedulerContext.escalation_limit,
        help="consecutive escalations before this role stops taking new work",
    )
    parser.add_argument(
        "--verify",
        default="",
        help=(
            "shell command run in this role's worktree after the worker reports done; a "
            "non-zero exit costs an attempt and its output goes into the retry"
        ),
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=verify.DEFAULT_VERIFY_TIMEOUT_SEC,
        help="seconds before the verify command is killed and treated as a failure",
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help=(
            "escalate instead of working once this role has spent N dollars on one work "
            "item; only meaningful on a backend that reports cost (claude, grok)"
        ),
    )
    parser.add_argument(
        "--worker-debug",
        action="store_true",
        help=(
            "write the backend CLI's own internal debug trace per attempt to "
            "<db-path-dir>/logs/agent-debug-<role>-attempt<N>[.log]"
        ),
    )
    parser.add_argument(
        "--no-status-bar",
        action="store_true",
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
        tokens=bar.status.tokens + result.tokens,
        target=result.target or bar.status.target,
        detail=detail,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    enable_unicode_output()
    configure_logging(args.log_file)

    ctx = build_context(args)
    state = SchedulerState()
    bar = attach_status_bar(ctx, args)

    bar.start(format_banner(ctx, args))
    log.info(
        "scheduler started role=%s branch=%s worker=%s model=%s worktree=%s",
        ctx.role,
        ctx.branch,
        ctx.definition.name,
        display_model(args, ctx.definition),
        ctx.worktree,
    )

    # Also protect schedulers started without the launcher.
    _worktree(ctx).ensure_generated_ignored()

    try:
        recover_stale_messages(ctx)
        return _run_loop(ctx, state, args, bar)
    finally:
        bar.close()


recover_stale_messages = recover_interrupted_work


def _run_loop(
    ctx: SchedulerContext,
    state: SchedulerState,
    args: argparse.Namespace,
    bar: pane_status.StatusBar,
) -> int:
    consecutive_errors = 0
    while True:
        result, consecutive_errors, exit_code = _try_cycle(
            ctx, state, args, bar, consecutive_errors
        )
        if exit_code is not None:
            return exit_code
        if result is None:
            continue
        exit_code = _after_cycle(ctx, state, args, bar, result)
        if exit_code is not None:
            return exit_code


def _after_cycle(ctx, state, args, bar, result) -> int | None:
    exit_code = _complete_cycle(ctx, state, args, bar, result)
    if exit_code is not None or state.halted:
        return exit_code
    if result.outcome == IDLE and _poll_was_interrupted(args.poll_interval):
        return 130
    return None


def _complete_cycle(ctx, state, args, bar, result) -> int | None:
    _record_cycle(bar, result)
    _sync_status_totals(ctx, bar, result)
    _log_cycle_result(result)
    if state.halted:
        return _park_halted(ctx, state, args)
    state.parked = False
    return 0 if args.once else None


def _try_cycle(
    ctx: SchedulerContext,
    state: SchedulerState,
    args: argparse.Namespace,
    bar: pane_status.StatusBar,
    consecutive_errors: int,
) -> tuple[CycleResult | None, int, int | None]:
    try:
        return run_once(ctx, state), 0, None
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
        return None, consecutive_errors, 130
    except Exception:
        consecutive_errors += 1
        log.exception(
            f"{ICON_BLOCKED} cycle failed (%d/%d consecutive)",
            consecutive_errors,
            MAX_CONSECUTIVE_ERRORS,
        )
        # The context callback also writes the status file used by terminal tab badges.
        ctx.set_status("blocked")
        bar.update(detail=f"cycle failed ({consecutive_errors})")
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            log.error(f"{ICON_HALT} too many consecutive failures; exiting")
            ctx.set_status("halted")
            return None, consecutive_errors, 1
        time.sleep(min(args.poll_interval * consecutive_errors, 30))
        return None, consecutive_errors, None


def _log_cycle_result(result: CycleResult) -> None:
    if result.outcome not in (IDLE, HALTED):
        log.info("cycle -> %s %s", result.outcome, result.detail)


def _park_halted(
    ctx: SchedulerContext, state: SchedulerState, args: argparse.Namespace
) -> int | None:
    # Park instead of exiting so `kiln retry` can still reach this scheduler.
    if not state.parked:
        state.parked = True
        log.error(f"{ICON_HALT} scheduler halted; waiting for `kiln retry <message-id>`")
    ctx.set_status("halted")
    if args.once:
        return 1
    return 130 if _poll_was_interrupted(args.poll_interval) else None


def _poll_was_interrupted(interval: float) -> bool:
    try:
        time.sleep(interval)
        return False
    except KeyboardInterrupt:
        # The poll sleep is where an idle scheduler spends nearly all its time.
        log.info("interrupted; shutting down")
        return True


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
