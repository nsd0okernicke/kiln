"""Public, backend-neutral knowledge catalog and search commands."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ..application.knowledge_service import KnowledgeService, result_dict
from ..domain import catalog, documents, web
from ..domain.models import LOCAL_TYPES, URL_TYPE, KnowledgeError, Source
from .factory import knowledge_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiln knowledge")
    parser.add_argument("--working-dir", "-WorkingDir", default=".")
    subparsers = parser.add_subparsers(dest="action", required=True)

    setup = subparsers.add_parser("setup", help="discover candidate local knowledge sources")
    setup.add_argument("--json", action="store_true")

    add = subparsers.add_parser("add", help="add an approved source to the catalog")
    add.add_argument("path", metavar="PATH_OR_URL", help="project-relative path, or an http(s) url")
    add.add_argument("--id")
    add.add_argument("--title")
    add.add_argument("--type", choices=[*LOCAL_TYPES, URL_TYPE])
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--json", action="store_true")

    remove = subparsers.add_parser("remove", help="remove a source from the catalog")
    remove.add_argument("source_id")
    remove.add_argument("--json", action="store_true")

    sources = subparsers.add_parser("sources", help="list approved sources")
    sources.add_argument("--json", action="store_true")

    sync = subparsers.add_parser("sync", help="incrementally rebuild stale knowledge")
    sync.add_argument(
        "--offline",
        action="store_true",
        help="do not fetch url sources; they keep what they have and are listed as deferred",
    )
    sync.add_argument("--json", action="store_true")

    search = subparsers.add_parser("search", help="search indexed knowledge")
    search.add_argument("query")
    search.add_argument("--max-results", type=int, default=10)
    search.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="show one indexed document")
    show.add_argument("document_id")
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.working_dir).resolve()
    try:
        with knowledge_service(project_root, offline=getattr(args, "offline", False)) as service:
            return _execute(service, project_root, args)
    except KnowledgeError as exc:
        print(f"Error: {exc}")
        return 1


def _execute(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    handlers = {
        "setup": _setup,
        "add": _add,
        "remove": _remove,
        "sources": _sources,
        "sync": _sync,
        "search": _search,
        "show": _show,
    }
    return handlers[args.action](service, project_root, args)


def _setup(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    _emit(service.candidates(), args.json, _candidate_line)
    return 0


def _add(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    source = _source_from_args(project_root, args)
    service.add(source)
    _emit(
        catalog.source_dict(source),
        args.json,
        lambda value: f"added {value['id']}: {value.get('url') or value.get('path', '')}",
    )
    return 0


def _remove(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    service.remove(args.source_id)
    _emit({"removed": args.source_id}, args.json, lambda value: f"removed {value['removed']}")
    return 0


def _sources(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    _emit([catalog.source_dict(source) for source in service.sources()], args.json, _source_line)
    return 0


def _sync(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    result = service.sync()
    _emit(result.as_dict(), args.json, _sync_line)
    # Deferred sources are not a failure: `--offline` is a thing you asked for.
    return 1 if result.failures else 0


def _search(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    if not 1 <= args.max_results <= 100:
        raise KnowledgeError("--max-results must be between 1 and 100")
    results = [result_dict(result) for result in service.search(args.query, args.max_results)]
    _emit(results, args.json, _result_line)
    return 0


def _show(service: KnowledgeService, project_root: Path, args: argparse.Namespace) -> int:
    _emit(service.show(args.document_id), args.json, _document_line)
    return 0


def _source_from_args(project_root: Path, args: argparse.Namespace) -> Source:
    """
    A catalog entry from the command line, routed by whether the argument is a url.

    The scheme decides, so `kiln knowledge add https://...` needs no `--type` -- and an
    explicit `--type url` still works for anyone who prefers to say it.
    """
    if web.looks_like_url(args.path) or args.type == URL_TYPE:
        return _url_source(args)
    return _local_source(project_root, args)


def _local_source(project_root: Path, args: argparse.Namespace) -> Source:
    candidate = Path(args.path)
    relative, resolved = _relative_path(project_root, candidate)
    source_type = args.type or _infer_type(resolved)
    source_id = args.id or _slug(candidate.stem or candidate.name)
    title = args.title or _default_title(candidate, source_id)
    return Source(source_id, relative, title, source_type, tuple(args.tag))


def _url_source(args: argparse.Namespace) -> Source:
    url = web.validate_url(args.path)
    title = args.title or web.default_title(url)
    return Source(args.id or _slug(title), "", title, URL_TYPE, tuple(args.tag), url)


def _relative_path(project_root: Path, candidate: Path) -> tuple[str, Path]:
    resolved = (project_root / candidate).resolve(strict=False)
    try:
        relative = resolved.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise KnowledgeError(f"knowledge source escapes the project: {candidate}") from exc
    return relative, resolved


def _default_title(candidate: Path, source_id: str) -> str:
    title = candidate.stem.replace("-", " ").replace("_", " ").strip().title()
    return title or source_id


def _infer_type(path: Path) -> str:
    if path.is_dir():
        return "directory"
    source_type = documents.media_type(path.suffix)
    if not source_type:
        raise KnowledgeError(f"cannot infer knowledge source type from {path.name}")
    return source_type


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise KnowledgeError("could not derive a source id; pass --id")
    return slug[:64]


def _emit(value, as_json: bool, formatter) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    elif isinstance(value, list):
        print("\n".join(formatter(item) for item in value) or "No knowledge sources found.")
    else:
        print(formatter(value))


def _candidate_line(value: dict) -> str:
    return f"{value['type']:<8} {value['path']}"


def _source_line(value: dict) -> str:
    return f"{value['id']:<20} {value['type']:<9} {value.get('url') or value.get('path', '')}"


def _sync_line(value: dict) -> str:
    summary = (
        f"updated={value['updated']} skipped={value['skipped']} "
        f"removed={value['removed']} failed={value['failed']}"
    )
    lines = [summary]
    if value.get("deferred"):
        # Named rather than counted: "which sources are stale" is the question --offline raises.
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
    return f"{value['title']}\n{value['path']}\n\n{value['content']}"
