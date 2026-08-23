"""Concrete scheduler/launcher adapter for cockpit operator actions."""

from pathlib import Path

from kiln.launcher.infrastructure import stop
from kiln.scheduler.infrastructure.cli import dashboard, retry, send
from kiln.scheduler.infrastructure.persistence import db

from ..application.ports import Session


class KilnActionGateway:
    def sessions(self, path: Path) -> list[Session]:
        return [Session(item.role, item.passive) for item in dashboard.read_sessions(path)]

    def send(self, **kwargs) -> str:
        return send.send(**kwargs)

    def retry(self, **kwargs) -> dict | None:
        return retry.resume(**kwargs)

    def message(self, db_path: Path, message_id: str) -> dict | None:
        return db.get_message(db_path, message_id)

    def failed_messages(self, db_path: Path, branch: str) -> list[dict]:
        return db.failed_messages(db_path, branch)

    def stop_all(self, roles: list[str]) -> list[int]:
        return stop.stop_all(roles)
