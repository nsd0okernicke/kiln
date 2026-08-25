"""Manage the human-owned backlog from an operator or HITL agent shell."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ...application import backlog
from ...domain.routing import load_routing_table
from ..persistence import db, task_store

HUMAN_ROLE = "human-in-the-loop"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiln task", description="Manage HITL backlog tasks.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--working-dir", default=".")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a backlog task")
    create.add_argument("work_item")
    create.add_argument("--title", required=True)
    create.add_argument("--body", required=True)

    listing = commands.add_parser("list", help="list tasks")
    listing.add_argument("--status", choices=("backlog", "active", "archived"), default="backlog")
    listing.add_argument("--all", action="store_true")

    show = commands.add_parser("show", help="show one task")
    show.add_argument("task")

    update = commands.add_parser("update", help="edit a backlog task")
    update.add_argument("task")
    update.add_argument("--title")
    update.add_argument("--body")

    handoff = commands.add_parser("handoff", help="dispatch a backlog task")
    handoff.add_argument("task")
    handoff.add_argument("--to", dest="target")

    archive = commands.add_parser("archive", help="archive a backlog task")
    archive.add_argument("task")
    return parser


def _check_actor(db_path: Path, branch: str) -> str:
    context = task_store.get_context(db_path, branch=branch)
    human_role = str((context or {}).get("human_role") or HUMAN_ROLE)
    role = os.getenv("KILN_ROLE", "").strip()
    if role and role != human_role:
        raise backlog.BacklogError(
            f"backlog mutation belongs to {human_role!r}; this session identifies as {role!r}"
        )
    return human_role


def _default_target(db_path: Path, branch: str, working_dir: str, human_role: str) -> str:
    context = task_store.get_context(db_path, branch=branch)
    if context and context["intake_role"]:
        return str(context["intake_role"])
    workflow = Path(working_dir).resolve() / "kiln" / "project" / "constitution" / "workflow.md"
    target = load_routing_table(workflow).resolve(human_role)
    if not target:
        raise backlog.BacklogError(
            "no intake route for human-in-the-loop; pass --to or configure its handoff route"
        )
    return target


def _execute(args: argparse.Namespace) -> dict | list[dict]:
    handler = _COMMAND_HANDLERS[args.command]
    return handler(args, Path(args.db_path), args.branch)


def _create(args: argparse.Namespace, db_path: Path, branch: str) -> dict:
    _check_actor(db_path, branch)
    return backlog.create(
        db_path, branch=branch, work_item=args.work_item, title=args.title, body=args.body
    )


def _list(args: argparse.Namespace, db_path: Path, branch: str) -> list[dict]:
    return backlog.list_all(db_path, branch=branch, status=None if args.all else args.status)


def _show(args: argparse.Namespace, db_path: Path, branch: str) -> dict:
    return backlog.show(db_path, branch=branch, identifier=args.task)


def _update(args: argparse.Namespace, db_path: Path, branch: str) -> dict:
    _check_actor(db_path, branch)
    if args.title is None and args.body is None:
        raise backlog.BacklogError("update needs --title, --body, or both")
    return backlog.update(
        db_path,
        branch=branch,
        identifier=args.task,
        title=args.title,
        body=args.body,
    )


def _handoff(args: argparse.Namespace, db_path: Path, branch: str) -> dict:
    human_role = _check_actor(db_path, branch)
    target = args.target or _default_target(db_path, branch, args.working_dir, human_role)
    return backlog.handoff(
        db_path,
        branch=branch,
        identifier=args.task,
        sender=human_role,
        target=target,
    )


def _archive(args: argparse.Namespace, db_path: Path, branch: str) -> dict:
    _check_actor(db_path, branch)
    return backlog.archive(db_path, branch=branch, identifier=args.task)


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, Path, str], dict | list[dict]]] = {
    "create": _create,
    "list": _list,
    "show": _show,
    "update": _update,
    "handoff": _handoff,
    "archive": _archive,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not Path(args.db_path).is_file():
        print(
            f"error: no message queue at {args.db_path}. Launch the swarm first.", file=sys.stderr
        )
        return 1
    db.ensure_schema(args.db_path)
    try:
        result = _execute(args)
    except (backlog.BacklogError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
