"""SQLite implementation of the application message-queue port."""

import sqlite3
from pathlib import Path

from ...application.ports import QueueAccessError
from ...domain.models import DEFAULT_PRIORITY, InboundMessage, QueueMessage
from . import queue_commands, task_store
from .queue_queries import count_work_item_arrivals


class SQLiteMessageQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, role: str, branch: str) -> InboundMessage | None:
        return queue_commands.fetch_and_deliver(self.path, role, branch)

    def fetch_resume(self, role: str, branch: str) -> InboundMessage | None:
        return queue_commands.fetch_resume(self.path, role, branch)

    def mark_processing(self, message_id: str) -> bool:
        return queue_commands.mark_processing(self.path, message_id)

    def mark_processed(self, message_id: str) -> bool:
        return queue_commands.mark_processed(self.path, message_id)

    def mark_failed(self, message_id: str, error: str) -> bool:
        return queue_commands.mark_failed(self.path, message_id, error)

    def name_work_item(self, message_id: str, work_item: str) -> bool:
        return queue_commands.name_work_item(self.path, message_id, work_item)

    def count_arrivals(self, work_item: str, branch: str, target: str) -> int:
        return count_work_item_arrivals(self.path, work_item, branch, target)

    def insert(
        self,
        sender: str,
        target: str,
        content: str,
        branch: str,
        priority: int = DEFAULT_PRIORITY,
        work_item: str | None = None,
    ) -> str:
        return queue_commands.insert_handoff(
            self.path, sender, target, content, branch, priority, work_item
        )

    def exists(self, message_id: str) -> bool:
        return queue_commands.message_exists(self.path, message_id)

    def recover_processing(self, role: str, branch: str) -> list[QueueMessage]:
        try:
            return queue_commands.recover_stale_processing(self.path, role, branch)
        except sqlite3.Error as exc:
            raise QueueAccessError(str(exc)) from exc

    # --- Sequential-mode helpers ---

    def sequential_enabled(self, branch: str) -> bool:
        try:
            return task_store.get_sequential(self.path, branch=branch)
        except Exception:
            return False

    def next_backlog_task(self, branch: str) -> dict | None:
        try:
            return task_store.find_next_backlog_task(self.path, branch=branch)
        except Exception:
            return None

    def dispatch_backlog_task(
        self, identifier: str, branch: str, sender: str, target: str
    ) -> str:
        result = task_store.handoff_task(
            self.path, branch=branch, identifier=identifier,
            sender=sender, target=target,
        )
        return str(result["message_id"])

    def create_spec_defect_task(
        self, branch: str, work_item: str, failure_detail: str
    ) -> dict | None:
        return task_store.create_spec_defect_task(
            self.path, branch=branch, work_item=work_item, failure_detail=failure_detail
        )
