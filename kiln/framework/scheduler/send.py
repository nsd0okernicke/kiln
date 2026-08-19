"""
Send a handoff from the command line — the human's outbound half.

The wrapper-mode human role sent handoffs through the `kiln-db` MCP server, which meant a
person could only talk to their own swarm through an LLM session that had to be alive, in the
right step of its loop, and with a working MCP stack. When any of that failed, the message
simply did not get sent.

This is the same INSERT, reachable directly. It has no LLM in it and no MCP dependency, so a
human can start or unblock a cycle from any terminal — including when the swarm's agents are
the thing that is broken.

The commit is optional and usually absent: a human's opening request has no *new* work of its
own to merge. The receiving role still merges `--branch` (default `main`), because that branch
is where everything completed so far actually lives — without it a role handed work by a human
never catches up, and drifts one full cycle behind on every intake.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import db, handoff
from .status_contract import PENDING_HANDOFF

log = logging.getLogger(__name__)

__all__ = ["PENDING_HANDOFF", "build_message", "build_parser", "main", "send"]


def build_message(
    *,
    sender: str,
    target: str,
    summary: str,
    branch: str,
    handoff_name: str = PENDING_HANDOFF,
    commit: str = "",
    escalation: bool = False,
    timestamp: str | None = None,
) -> str:
    """Render the outbound message in workflow.md's format. Pure."""
    return handoff.format_handoff(
        sender=sender,
        handoff=handoff_name,
        branch=branch,
        commit=commit,
        summary=summary,
        next_role=target,
        timestamp=timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        escalation=escalation,
    )


def send(
    *,
    db_path: str | Path,
    sender: str,
    target: str,
    summary: str,
    branch: str,
    handoff_name: str = PENDING_HANDOFF,
    commit: str = "",
    escalation: bool = False,
    priority: int = db.DEFAULT_PRIORITY,
) -> str:
    """Queue one handoff. Returns the new message id."""
    content = build_message(
        sender=sender, target=target, summary=summary, branch=branch,
        handoff_name=handoff_name, commit=commit, escalation=escalation,
    )
    # `pending` is the placeholder a human uses for a brand-new request: the specifier is
    # what invents the real name, so there is nothing to group by yet and NULL is correct.
    # Case-insensitively, matching `role_scheduler.is_pending` -- a human typing `Pending`
    # must not create a second, indistinguishable placeholder bucket.
    work_item = None if handoff_name.strip().lower() == PENDING_HANDOFF else handoff_name
    message_id = db.insert_handoff(
        db_path, sender, target, content, branch, priority, work_item=work_item
    )
    log.info("queued %s -> %s (id=%s)", sender, target, message_id[:8])
    return message_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln send", description="Queue a handoff for another Kiln role."
    )
    parser.add_argument("summary", help="what you want done, in one or more sentences")
    parser.add_argument("--to", dest="target", required=True, help="receiving role")
    parser.add_argument("--from", dest="sender", default="human-in-the-loop")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--handoff", default=PENDING_HANDOFF,
        help=f"specifier handoff name; leave as {PENDING_HANDOFF!r} for a new request",
    )
    parser.add_argument("--commit", default="", help="commit to merge, if you have one")
    parser.add_argument("--priority", type=int, default=db.DEFAULT_PRIORITY)
    parser.add_argument("--escalation", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # WARNING, not INFO: `db.insert_handoff` and `send()` both log this same event, and a
    # human running one command wants one line of confirmation, not three.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    db_path = Path(args.db_path)
    if not db_path.is_file():
        # Almost always "the swarm was never launched here", which is worth saying plainly
        # rather than surfacing as a sqlite error about a file it silently created.
        print(f"error: no message queue at {db_path}. Launch the swarm first.", file=sys.stderr)
        return 1

    message_id = send(
        db_path=db_path,
        sender=args.sender,
        target=args.target,
        summary=args.summary,
        branch=args.branch,
        handoff_name=args.handoff,
        commit=args.commit,
        escalation=args.escalation,
        priority=args.priority,
    )
    print(f"queued {args.sender} -> {args.target}  (id={message_id[:8]})")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
