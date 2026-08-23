"""SQLite implementation of the application message-queue port."""

import sqlite3
from pathlib import Path

from ...application.ports import QueueAccessError
from ...domain.models import DEFAULT_PRIORITY, InboundMessage, QueueMessage
from . import queue_commands
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
