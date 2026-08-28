"""Ports used by the cockpit's operator actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Session:
    role: str
    passive: bool = False


class TaskActionError(Exception):
    """A backlog request rejected by the scheduler task service."""


class ActionGateway(Protocol):
    """Infrastructure capabilities required by cockpit write use cases."""

    def sessions(self, path: Path) -> list[Session]: ...

    def send(
        self,
        *,
        db_path: Path,
        sender: str,
        target: str,
        summary: str,
        branch: str,
        handoff_name: str,
    ) -> str: ...

    def retry(self, *, db_path: Path, message_id: str, guidance: str) -> dict | None: ...

    def message(self, db_path: Path, message_id: str) -> dict | None: ...

    def failed_messages(self, db_path: Path, branch: str) -> list[dict]: ...

    def create_task(self, **kwargs) -> dict: ...

    def update_task(self, **kwargs) -> dict: ...

    def handoff_task(self, **kwargs) -> dict: ...

    def archive_task(self, **kwargs) -> dict: ...

    def sequential_enabled(self, *, db_path: Path, branch: str) -> bool: ...
    def set_sequential(self, *, db_path: Path, branch: str, enabled: bool) -> None: ...
    def stop_all(self, roles: list[str]) -> list[int]: ...
