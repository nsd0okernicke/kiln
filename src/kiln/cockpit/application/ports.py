"""Ports used by the cockpit's operator actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Session:
    role: str
    passive: bool = False


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

    def stop_all(self, roles: list[str]) -> list[int]: ...
