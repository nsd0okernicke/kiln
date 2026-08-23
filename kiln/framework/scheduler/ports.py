"""Small scheduler ports and adapters over Kiln's existing infrastructure modules."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from . import db, git_ops
from .adapters import WorkerInvocation
from .models import QueueMessage


class MessageQueue(Protocol):
    def fetch(self, role: str, branch: str) -> QueueMessage | None: ...
    def fetch_resume(self, role: str, branch: str) -> QueueMessage | None: ...
    def mark_processing(self, message_id: str) -> bool: ...
    def mark_processed(self, message_id: str) -> bool: ...
    def mark_failed(self, message_id: str, error: str) -> bool: ...
    def count_arrivals(self, work_item: str, branch: str, target: str) -> int: ...
    def insert(
        self,
        sender: str,
        target: str,
        content: str,
        branch: str,
        priority: int = db.DEFAULT_PRIORITY,
        work_item: str | None = None,
    ) -> str: ...
    def exists(self, message_id: str) -> bool: ...
    def recover_processing(self, role: str, branch: str) -> list[QueueMessage]: ...


class Worktree(Protocol):
    def already_contains(self, target: str) -> bool: ...
    def merge(self, target: str, message: str): ...
    def squash_anchor(self) -> str: ...
    def has_commits_since(self, anchor: str) -> bool: ...
    def has_pending_changes(self) -> bool: ...
    def squash_since(self, anchor: str, message: str): ...
    def head_commit(self) -> str: ...
    def ensure_generated_ignored(self) -> None: ...


class WorkerRunner(Protocol):
    def __call__(self, **kwargs: object) -> WorkerInvocation: ...


class SQLiteMessageQueue:
    """MessageQueue adapter retaining the existing SQLite behavior verbatim."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, role: str, branch: str) -> QueueMessage | None:
        return db.fetch_and_deliver(self.path, role, branch)

    def fetch_resume(self, role: str, branch: str) -> QueueMessage | None:
        return db.fetch_resume(self.path, role, branch)

    def mark_processing(self, message_id: str) -> bool:
        return db.mark_processing(self.path, message_id)

    def mark_processed(self, message_id: str) -> bool:
        return db.mark_processed(self.path, message_id)

    def mark_failed(self, message_id: str, error: str) -> bool:
        return db.mark_failed(self.path, message_id, error)

    def count_arrivals(self, work_item: str, branch: str, target: str) -> int:
        return db.count_work_item_arrivals(self.path, work_item, branch, target)

    def insert(
        self,
        sender: str,
        target: str,
        content: str,
        branch: str,
        priority: int = db.DEFAULT_PRIORITY,
        work_item: str | None = None,
    ) -> str:
        return db.insert_handoff(self.path, sender, target, content, branch, priority, work_item)

    def exists(self, message_id: str) -> bool:
        return db.message_exists(self.path, message_id)

    def recover_processing(self, role: str, branch: str) -> list[QueueMessage]:
        return db.recover_stale_processing(self.path, role, branch)


class GitWorktree:
    """Worktree adapter bound to one repository path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def already_contains(self, target: str) -> bool:
        return git_ops.already_contains(target, self.path)

    def merge(self, target: str, message: str):
        return git_ops.merge_commit(target, self.path, message=message)

    def squash_anchor(self) -> str:
        return git_ops.squash_anchor(self.path)

    def has_commits_since(self, anchor: str) -> bool:
        return git_ops.has_commits_since(anchor, self.path)

    def has_pending_changes(self) -> bool:
        return git_ops.has_pending_changes(self.path)

    def squash_since(self, anchor: str, message: str):
        return git_ops.squash_since(anchor, message, self.path)

    def head_commit(self) -> str:
        return git_ops.head_commit(self.path)

    def ensure_generated_ignored(self) -> None:
        git_ops.ensure_generated_ignored(self.path)


WorkerCallable = Callable[..., WorkerInvocation]
