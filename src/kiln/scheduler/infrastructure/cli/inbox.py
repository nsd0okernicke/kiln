"""Non-blocking queue listener for messages addressed to a human role."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ...application.use_cases.process_next_message import merge_commit_message
from ...domain import handoff
from ..persistence import db
from ..runtime import configure_logging, enable_unicode_output
from ..terminal import pane_status
from ..vcs import GitWorktree
from ..vcs import git as git_ops

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 2.0

ICON_MESSAGE = "\N{ENVELOPE}"
ICON_ESCALATION = "\N{POLICE CARS REVOLVING LIGHT}"
ICON_PING = "\N{TABLE TENNIS PADDLE AND BALL}"
ICON_START = "\N{INBOX TRAY}"
ICON_MERGE = "\N{TWISTED RIGHTWARDS ARROWS}"
ICON_MERGE_FAILED = "\N{NO ENTRY}"

#: ASCII BEL. A human is not watching this pane — that is the whole point of it existing —
#: so an arriving message rings the terminal rather than relying on being seen.
BELL = "\a"


def persist_inbound(worktree: str | Path, content: str) -> Path | None:
    """Persist the human inbox's inbound handoff in its worktree."""
    return GitWorktree(worktree).persist_inbound(content)


@dataclass
class InboxContext:
    """Everything the loop needs. Injected so tests drive it with no real terminal."""

    role: str
    branch: str
    db_path: Path
    #: Where this human works — the project root, since the role is `@current`. The inbound
    #: commit is merged here, so the work actually lands in their tree.
    worktree: Path | None = None
    merge: bool = True
    bell: bool = True
    emit: object = None  # Callable[[str], None]; defaults to print

    def write(self, line: str) -> None:
        (self.emit or _default_emit)(line)


def _default_emit(line: str) -> None:
    print(line, flush=True)


def classify(inbound: handoff.InboundHandoff, raw: str) -> str:
    """`escalation`, `ping` or `handoff` — decides the icon and the bar colour."""
    if handoff.is_escalation(raw):
        return "escalation"
    if inbound.is_ping:
        return "ping"
    return "handoff"


def format_message(message: dict, kind: str) -> list[str]:
    """
    Render one message for a human reading a terminal.

    The stored content is already the workflow.md handoff format, which is readable as-is;
    this adds the framing a person needs — who and when, and whether it is an emergency —
    without reformatting the body and risking losing something.
    """
    icon = {
        "escalation": ICON_ESCALATION,
        "ping": ICON_PING,
    }.get(kind, ICON_MESSAGE)
    label = "ESCALATION" if kind == "escalation" else kind.upper()

    header = f"{icon}  {label} from {message.get('sender', '?')}"
    if message.get("created_at"):
        header += f"   {message['created_at']}"

    body = str(message.get("content", "")).rstrip()
    return [handoff.SEPARATOR, header, handoff.SEPARATOR, body, ""]


def receive(ctx: InboxContext, inbound: handoff.InboundHandoff, raw: str) -> list[str]:
    """
    The deterministic half of `/kiln-receive`, for a human: persist, then squash-merge.

    Showing a person a message is not the same as delivering it. `human-in-the-loop` is a
    real role in the graph — it works in the project root on the *real* branch (`@current`,
    not a disposable sub-branch) — so an inbound handoff has to land in their tree or the
    work they are being asked to review is not there. Step 1 of the skill writes
    `tmp/handoff-in.md`; step 4 applies the commit — via `git_ops.squash_merge_commit`, not a
    real merge, so the sender's full branch history does not become part of this branch's
    permanent ancestry every single cycle.

    Returns lines to show. Never raises: a merge that fails is something a human fixes, and
    telling them beats crashing the only channel that could have told them.
    """
    lines: list[str] = []
    if ctx.worktree is None:
        return lines

    written = persist_inbound(ctx.worktree, raw)
    if written is not None:
        lines.append(f"   saved to {written}")

    if not (ctx.merge and inbound.is_mergeable):
        return lines

    # `merge_target`, not `commit`: a message may name only the branch its work is on.
    target = inbound.merge_target
    result = git_ops.squash_merge_commit(
        target, ctx.worktree, message=merge_commit_message(ctx.role, inbound)
    )
    lines.extend(_merge_result_lines(ctx, inbound, target, result))
    return lines


def _merge_result_lines(
    ctx: InboxContext,
    inbound: handoff.InboundHandoff,
    target: str,
    result: git_ops.GitResult,
) -> list[str]:
    if result.ok:
        log.info("squash-merged %s into %s", target[:8], ctx.branch)
        return [f"   {ICON_MERGE} squash-merged {target[:8]} into {ctx.branch}"]
    log.error("merge of %s failed: %s", target, result.output)
    return [
        f"   {ICON_MERGE_FAILED} MERGE FAILED for {inbound.commit[:8]}:",
        *(f"      {line}" for line in result.output.splitlines()),
        "      the work above is NOT in your tree; resolve before continuing",
    ]


def poll_once(ctx: InboxContext, bar: pane_status.StatusBar | None = None) -> dict | None:
    """
    Receive and display at most one message. Returns it, or None when the inbox is empty.

    A displayed message is marked `processed` — including one whose merge failed. For an
    agent that would be a lie, but for a human "processed" means "shown to them", and their
    reply is a new message. Leaving it `delivered` would make `fetch_and_deliver` re-serve
    it on every poll forever, which is why a failed merge is shouted about in the pane
    rather than left in the queue to be retried by nobody.
    """
    message = db.fetch_and_deliver(ctx.db_path, ctx.role, ctx.branch)
    if message is None:
        return None

    raw = str(message.get("content", ""))
    inbound = handoff.parse_handoff(raw)
    kind = classify(inbound, raw)

    log.info("%s from %s (id=%s)", kind, message.get("sender", "?"), str(message.get("id"))[:8])
    received = receive(ctx, inbound, raw)
    merge_failed = any("MERGE FAILED" in line for line in received)

    _display_message(ctx, message, kind, received)
    db.mark_processed(ctx.db_path, message["id"])
    _update_bar(bar, message, kind, merge_failed)
    return message


def _display_message(ctx: InboxContext, message: dict, kind: str, received: list[str]) -> None:
    if ctx.bell:
        ctx.write(BELL)
    for line in [*format_message(message, kind), *received]:
        ctx.write(line)
    if received:
        ctx.write("")


def _update_bar(
    bar: pane_status.StatusBar | None, message: dict, kind: str, merge_failed: bool
) -> None:
    if bar is None:
        return
    bar.update(
        state="blocked" if (kind == "escalation" or merge_failed) else "receiving",
        cycles=bar.status.cycles + 1,
        detail=f"{kind} from {message.get('sender', '?')}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln inbox", description="Show messages addressed to a human role."
    )
    parser.add_argument("--role", default="human-in-the-loop")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--db-path", required=True)
    parser.add_argument(
        "--worktree",
        default=None,
        help="where to persist tmp/handoff-in.md and merge inbound commits "
        "(the human's working directory; omit to only display)",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="show messages without merging their commits",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--once", action="store_true", help="drain the inbox and exit")
    parser.add_argument("--no-bell", action="store_true", help="do not ring the terminal")
    parser.add_argument("--no-status-bar", action="store_true")
    return parser


def format_banner(ctx: InboxContext, args: argparse.Namespace) -> list[str]:
    fields = [
        ("watching", ctx.role),
        ("branch", ctx.branch),
        ("queue", str(ctx.db_path)),
        ("poll", f"{args.poll_interval:g}s"),
    ]
    if ctx.worktree is not None:
        fields.insert(2, ("worktree", str(ctx.worktree)))
        fields.insert(3, ("merges", "yes" if ctx.merge else "no (display only)"))
    width = max(len(name) for name, _ in fields)
    rule = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * (width + 44)
    return [
        f"{ICON_START} Kiln inbox",
        rule,
        *(f"  {name:<{width}}  {value}" for name, value in fields),
        rule,
        "",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    enable_unicode_output()
    configure_logging(args.log_file, label="kiln-inbox")

    ctx = InboxContext(
        role=args.role,
        branch=args.branch,
        db_path=Path(args.db_path),
        worktree=Path(args.worktree) if args.worktree else None,
        merge=not args.no_merge,
        bell=not args.no_bell,
    )
    bar = pane_status.StatusBar(
        pane_status.PaneStatus(role=f"{ctx.role} inbox", state="waiting"),
        enabled=False if args.no_status_bar else None,
    )
    bar.start(format_banner(ctx, args))

    waiting = db.count_queued(ctx.db_path, ctx.role, ctx.branch)
    if waiting:
        log.info("%d message(s) already waiting", waiting)

    try:
        return _poll_loop(ctx, bar, once=args.once, poll_interval=args.poll_interval)
    finally:
        bar.close()


def _poll_loop(
    ctx: InboxContext, bar: pane_status.StatusBar, *, once: bool, poll_interval: float
) -> int:
    while True:
        try:
            message = poll_once(ctx, bar)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
            return 130
        except Exception:
            # An inbox that dies takes the human's only notification channel with it.
            log.exception("poll failed; continuing")
            message = None
        exit_code = _after_poll(message, bar, once=once, poll_interval=poll_interval)
        if exit_code is not None:
            return exit_code


def _after_poll(message, bar, *, once: bool, poll_interval: float) -> int | None:
    if message is not None:
        return None
    if once:
        return 0
    bar.update(state="waiting")
    return None if _sleep_until_next_poll(poll_interval) else 130


def _sleep_until_next_poll(poll_interval: float) -> bool:
    try:
        time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
        return False
    return True


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
