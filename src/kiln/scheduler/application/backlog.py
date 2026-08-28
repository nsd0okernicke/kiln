"""Human-owned backlog use cases shared by the CLI and Cockpit."""

from __future__ import annotations

from pathlib import Path

from ..domain.status_contract import is_valid_work_item_name
from ..infrastructure.persistence import task_store


class BacklogError(Exception):
    """A user-facing invalid backlog operation."""


def create(db_path: str | Path, *, branch: str, work_item: str, title: str, body: str) -> dict:
    work_item, title, body = work_item.strip(), title.strip(), body.strip()
    if not is_valid_work_item_name(work_item):
        raise BacklogError(
            "work-item name must start with a letter or digit and contain only letters, "
            "digits, spaces and . _ - / (80 characters at most)"
        )
    if not title:
        raise BacklogError("a task needs a title")
    if not body:
        raise BacklogError("a task needs a body")
    try:
        return task_store.create_task(
            db_path, branch=branch, work_item=work_item, title=title, body=body
        )
    except task_store.TaskConflictError as exc:
        raise BacklogError(str(exc)) from exc


def list_all(db_path: str | Path, *, branch: str, status: str | None = None) -> list[dict]:
    if status and status not in {
        task_store.TASK_BACKLOG,
        task_store.TASK_ACTIVE,
        task_store.TASK_ARCHIVED,
    }:
        raise BacklogError(f"unknown task status {status!r}")
    return task_store.list_tasks(db_path, branch=branch, status=status)


def show(db_path: str | Path, *, branch: str, identifier: str) -> dict:
    task = task_store.get_task(db_path, branch=branch, identifier=identifier.strip())
    if task is None:
        raise BacklogError(f"task {identifier!r} was not found")
    return task


def update(
    db_path: str | Path,
    *,
    branch: str,
    identifier: str,
    title: str | None = None,
    body: str | None = None,
) -> dict:
    current = show(db_path, branch=branch, identifier=identifier)
    new_title = _required_update_value(current["title"], title, "title")
    new_body = _required_update_value(current["body"], body, "body")
    try:
        return task_store.update_task(
            db_path,
            branch=branch,
            identifier=identifier,
            title=new_title,
            body=new_body,
        )
    except task_store.TaskConflictError as exc:
        raise BacklogError(str(exc)) from exc


def _required_update_value(current: str, replacement: str | None, field: str) -> str:
    value = current if replacement is None else replacement.strip()
    if not value:
        raise BacklogError(f"a task needs a {field}")
    return value


def archive(db_path: str | Path, *, branch: str, identifier: str) -> dict:
    try:
        return task_store.archive_task(db_path, branch=branch, identifier=identifier.strip())
    except task_store.TaskConflictError as exc:
        raise BacklogError(str(exc)) from exc


def sequential_enabled(db_path: str | Path, *, branch: str) -> bool:
    "Return whether sequential task execution is enabled for this branch."
    return task_store.get_sequential(db_path, branch=branch)


def set_sequential(
    db_path: str | Path, *, branch: str, enabled: bool
) -> None:
    "Enable or disable sequential task execution mode."
    task_store.set_sequential(db_path, branch=branch, enabled=enabled)


def handoff(
    db_path: str | Path,
    *,
    branch: str,
    identifier: str,
    sender: str,
    target: str,
) -> dict:
    try:
        return task_store.handoff_task(
            db_path,
            branch=branch,
            identifier=identifier,
            sender=sender,
            target=target,
        )
    except task_store.TaskConflictError as exc:
        raise BacklogError(str(exc)) from exc
