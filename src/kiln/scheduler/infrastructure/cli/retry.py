"""
Resume a role that escalated — the human's unblock half.

Escalation used to be a dead end. `_escalate` marked the inbound `processed`, sent a
`Kiln-Escalation: true` message to the human, and the thread ended there: the inbox pane is
display-and-merge only, so the only move left was `kiln send`, which starts a **new** work
item carrying none of the failed cycle's context.

Escalated messages are now marked `failed` with the reason in the `error` column, and this
puts one back in its own role's queue with the human's instructions attached. The **same row**
is re-queued rather than a new one inserted, so the work item, its lap count and its cost
history stay attached to one identity — a fresh row would look like brand-new work to every
guard that counts per work item.

`fetch_and_deliver` never selects `failed`, so nothing is re-served without going through
here. And `resume_failed` writes `acked_at`, which is what a halted scheduler polls for: a
role parked by the circuit breaker accepts an explicitly resumed message and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ...domain import handoff
from ..persistence import db

log = logging.getLogger(__name__)


def resume(*, db_path: str | Path, message_id: str, guidance: str) -> dict | None:
    """
    Re-queue one failed message with guidance attached. Returns the row, or None.

    None means the id named no failed message — either it does not exist, or it is not in a
    state that can be resumed. Refusing anything but `failed` is deliberate: re-queueing a
    message that is merely `processing` would hand a live scheduler a second copy of the work
    it is already doing.
    """
    message = db.get_message(db_path, message_id)
    if message is None or message["status"] != db.STATUS_FAILED:
        return None
    content = handoff.attach_guidance(str(message["content"]), guidance)
    return db.resume_failed(db_path, message_id, content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln retry",
        description="Send an escalated message back to the role that failed on it.",
    )
    parser.add_argument(
        "message_id",
        nargs="?",
        help="the failed message to resume; omit to list what failed",
    )
    parser.add_argument(
        "--guidance",
        default="",
        help="what the role should do differently — reaches the worker as its retry brief",
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list failed messages and exit",
    )
    return parser


def _print_failures(db_path: Path, branch: str) -> int:
    failures = db.failed_messages(db_path, branch)
    if not failures:
        print("no failed messages.")
        return 0
    print(f"{len(failures)} failed message(s) on {branch}:\n")
    for row in failures:
        work_item = row["work_item"] or "-"
        print(f"  {str(row['id'])[:8]}  {row['sender']} -> {row['target']}  [{work_item}]")
        print(f"            {row['error'] or '(no reason recorded)'}")
    print('\nresume one with: kiln retry <id> --guidance "..."')
    return 0


def _resolve_id(db_path: Path, branch: str, prefix: str) -> str | None:
    """
    Expand the short id `kiln retry --list` prints into the full one.

    The listing shows eight characters because a full uuid is unreadable in a terminal; asking
    a human to then type all 32 would make the listing useless.
    """
    matches = [
        str(row["id"])
        for row in db.failed_messages(db_path, branch)
        if str(row["id"]).startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"error: no failed message starting with {prefix!r}.", file=sys.stderr)
        return None
    print(f"error: {prefix!r} matches {len(matches)} failed messages.", file=sys.stderr)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    db_path = Path(args.db_path)
    if not db_path.is_file():
        print(f"error: no message queue at {db_path}. Launch the swarm first.", file=sys.stderr)
        return 1

    if args.list or not args.message_id:
        return _print_failures(db_path, args.branch)

    message_id = _resolve_id(db_path, args.branch, args.message_id)
    if message_id is None:
        return 1

    row = resume(db_path=db_path, message_id=message_id, guidance=args.guidance)
    if row is None:
        print(f"error: {args.message_id} is not a failed message.", file=sys.stderr)
        return 1

    print(f"resumed {str(row['id'])[:8]} -> {row['target']}")
    if not args.guidance:
        # Worth saying: the worker will retry with exactly the brief that already failed.
        print("note: no --guidance given, so the role retries with the original handoff.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
