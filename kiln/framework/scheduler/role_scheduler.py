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
from pathlib import Path

from . import application as scheduler_application
from . import pane_status, verify
from .adapters import DEFAULT_IDLE_TIMEOUT_SEC, TokenUsage, WorkerInvocation
from .application import CycleResult, SchedulerContext, SchedulerState
from .infrastructure import FileWorkerDebugSink, GitWorktree, SQLiteMessageQueue
from .models import WorkerRequest
from .routing import load_routing_table, parse_routing_arguments
from .worker_prompt import WorkerDefinition, load_worker_definition

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
        ("queue", ctx.queue_label),
        ("poll / worker timeout", f"{args.poll_interval:g}s / {args.worker_timeout}s"),
        ("idle timeout", f"{args.worker_idle_timeout:g}s" if args.worker_idle_timeout else "off"),
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
    from .adapters import claude_adapter, codex_adapter, copilot_adapter, grok_adapter

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
        # Only Claude's CLI takes a budget flag today; its adapter advertises that rather
        # than this function hardcoding a backend name. Grok reports cost but has no such
        # flag, so its cap is enforced by the scheduler's own tally alone.
        budget: dict[str, object] = {}
        if request.max_budget_usd is not None and getattr(adapter, "SUPPORTS_BUDGET_FLAG", False):
            budget["max_budget_usd"] = request.max_budget_usd
        return adapter.run_worker(
            definition=definition,
            prompt=request.prompt,
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
        worktree=Path(args.worktree),
        # A profile with its own routing sends it as --route arguments and replaces
        # workflow.md's table outright; without them the file stays the source.
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
        ctx.role,
        ctx.branch,
        ctx.definition.name,
        display_model(args, ctx.definition),
        ctx.worktree,
    )

    # The launcher normally covers this, but a scheduler started by hand — or in a project
    # scaffolded before these entries existed — would otherwise commit its own scaffolding.
    _worktree(ctx).ensure_generated_ignored()

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
        recovered = _queue(ctx).recover_processing(ctx.role, ctx.branch)
    except sqlite3.Error as exc:
        log.warning("could not check for messages left mid-cycle: %s", exc)
        return 0
    for row in recovered:
        log.warning(
            f"{ICON_RETRY} recovered message %s from %s (work item %s), left mid-cycle by a "
            "killed scheduler; re-serving it",
            str(row["id"])[:8],
            row["sender"] or "?",
            row["work_item"] or "-",
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
                consecutive_errors,
                MAX_CONSECUTIVE_ERRORS,
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
                log.error(f"{ICON_HALT} scheduler halted; waiting for `kiln retry <message-id>`")
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
