"""Public, backend-neutral vault knowledge status and search commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kiln.scheduler.infrastructure.runtime import enable_unicode_output

from ..application.knowledge_service import KnowledgeService, result_dict
from ..domain.models import KnowledgeError
from .factory import knowledge_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiln knowledge")
    parser.add_argument("--working-dir", "-WorkingDir", default=".")
    subparsers = parser.add_subparsers(dest="action", required=True)

    status = subparsers.add_parser("status", help="show the vault config and the last sync")
    status.add_argument("--json", action="store_true")

    sync = subparsers.add_parser("sync", help="incrementally index changed vault notes")
    sync.add_argument("--json", action="store_true")

    search = subparsers.add_parser("search", help="search indexed knowledge")
    search.add_argument("query")
    search.add_argument("--max-results", type=int, default=10)
    search.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="show one indexed note")
    show.add_argument("document_id")
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Vault notes carry Unicode (German terms, arrows); a cp1252 console must not stop a
    # search from being printed. Same rule the scheduler CLIs apply.
    enable_unicode_output()
    args = build_parser().parse_args(argv)
    project_root = Path(args.working_dir).resolve()
    try:
        with knowledge_service(project_root) as service:
            return _execute(service, args)
    except KnowledgeError as exc:
        print(f"Error: {exc}")
        return 1


def _execute(service: KnowledgeService, args: argparse.Namespace) -> int:
    handlers = {
        "status": _status,
        "sync": _sync,
        "search": _search,
        "show": _show,
    }
    return handlers[args.action](service, args)


def _status(service: KnowledgeService, args: argparse.Namespace) -> int:
    _emit(service.status(), args.json, _status_line)
    return 0


def _sync(service: KnowledgeService, args: argparse.Namespace) -> int:
    result = service.sync()
    _emit(result.as_dict(), args.json, _sync_line)
    # Deferred sources are not a failure: an unmounted vault is a state you are told about.
    return 1 if result.failures else 0


def _search(service: KnowledgeService, args: argparse.Namespace) -> int:
    if not 1 <= args.max_results <= 100:
        raise KnowledgeError("--max-results must be between 1 and 100")
    results = [result_dict(result) for result in service.search(args.query, args.max_results)]
    _emit(results, args.json, _result_line)
    return 0


def _show(service: KnowledgeService, args: argparse.Namespace) -> int:
    _emit(service.show(args.document_id), args.json, _document_line)
    return 0


def _emit(value, as_json: bool, formatter) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    elif isinstance(value, list):
        print("\n".join(formatter(item) for item in value) or "No results.")
    else:
        print(formatter(value))


def _status_line(value: dict) -> str:
    if not value.get("configured"):
        return 'knowledge vault is not configured; set "vault" in kiln/project/knowledge.json'
    return "\n".join(_status_facts(value))


def _status_facts(value: dict) -> list[str]:
    lines = [f"vault: {value['vault']}"]
    exclude = value.get("exclude") or []
    if exclude:
        lines.append("excluded: " + ", ".join(exclude))
    lines.append(_last_sync_line(value))
    lines.append(f"notes indexed: {value.get('documents', 0)}")
    failure = _failure_line(value)
    if failure:
        lines.append(failure)
    return lines


def _last_sync_line(value: dict) -> str:
    run = value.get("last_run")
    if not run:
        return "never synced; run `kiln knowledge sync`"
    finished = run.get("finished_at") or "in progress"
    return (
        f"last sync: {finished} "
        f"(updated={run['updated']} skipped={run['skipped']} "
        f"removed={run['removed']} failed={run['failed']})"
    )


def _failure_line(value: dict) -> str | None:
    source = value.get("source")
    if source and source.get("status") == "failed" and source.get("error"):
        return f"last sync failed: {source['error']}"
    return None


def _sync_line(value: dict) -> str:
    summary = (
        f"updated={value['updated']} skipped={value['skipped']} "
        f"removed={value['removed']} failed={value['failed']}"
    )
    lines = [summary]
    if value.get("deferred"):
        # Named rather than counted: "which origins are unread" is the question a deferral raises.
        lines.append("not refreshed: " + ", ".join(value["deferred"]))
    lines.extend(value["failures"])
    return "\n".join(lines)


def _result_line(value: dict) -> str:
    location = value["path"]
    if value["heading"]:
        location += f" — {value['heading']}"
    if value["page"]:
        location += f" — page {value['page']}"
    return f"[{value['document_id']}] {value['source_title']}\n{location}\n{value['excerpt']}"


def _document_line(value: dict) -> str:
    related = value.get("related") or []
    tail = f"\n\nRelated: {', '.join(related)}" if related else ""
    return f"{value['title']}\n{value['path']}\n\n{value['content']}{tail}"
