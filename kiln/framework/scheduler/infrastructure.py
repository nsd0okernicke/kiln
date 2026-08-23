"""Concrete adapters assembled by the scheduler composition root."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from . import db, git_ops
from .adapters import WorkerInvocation
from .models import DEFAULT_PRIORITY, InboundMessage, QueueMessage, WorkerRequest

log = logging.getLogger(__name__)


class CallableWorkerRunner:
    """Adapt the legacy keyword-callable worker shape to the typed application port."""

    def __init__(self, worker: Callable[..., WorkerInvocation]):
        self.worker = worker

    def __call__(self, request: WorkerRequest) -> WorkerInvocation:
        kwargs: dict[str, object] = {
            "prompt": request.prompt,
            "attempt": request.attempt,
        }
        if request.max_budget_usd is not None:
            kwargs["max_budget_usd"] = request.max_budget_usd
        return self.worker(**kwargs)


class SQLiteMessageQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, role: str, branch: str) -> InboundMessage | None:
        return db.fetch_and_deliver(self.path, role, branch)

    def fetch_resume(self, role: str, branch: str) -> InboundMessage | None:
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
        priority: int = DEFAULT_PRIORITY,
        work_item: str | None = None,
    ) -> str:
        return db.insert_handoff(self.path, sender, target, content, branch, priority, work_item)

    def exists(self, message_id: str) -> bool:
        return db.message_exists(self.path, message_id)

    def recover_processing(self, role: str, branch: str) -> list[QueueMessage]:
        return db.recover_stale_processing(self.path, role, branch)


class GitWorktree:
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

    def persist_inbound(self, content: str) -> Path | None:
        try:
            self.ensure_generated_ignored()
            target = self.path / "tmp" / "handoff-in.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return target
        except OSError as exc:
            log.warning("could not write tmp/handoff-in.md: %s", exc)
            return None
