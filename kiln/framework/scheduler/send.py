"""
Send a handoff from the command line — the human's outbound half.

The wrapper-mode human role sent handoffs through the `kiln-db` MCP server, which meant a
person could only talk to their own swarm through an LLM session that had to be alive, in the
right step of its loop, and with a working MCP stack. When any of that failed, the message
simply did not get sent.

This is the same INSERT, reachable directly. It has no LLM in it and no MCP dependency, so a
human can start or unblock a cycle from any terminal — including when the swarm's agents are
the thing that is broken.

The commit is optional and usually absent: a human's opening request has no work to merge.
Downstream roles handle that already — `InboundHandoff.is_mergeable` is false without one,
which is the same path a ping takes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import db, handoff

log = logging.getLogger(__name__)

#: What the specifier replaces with a real handoff name once it accepts the request. Matches
#: the placeholder the human-in-the-loop role file tells a person to use.
PENDING_HANDOFF = "pending"


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
    message_id = db.insert_handoff(db_path, sender, target, content, branch, priority)
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
