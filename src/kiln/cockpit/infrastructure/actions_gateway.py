"""Concrete scheduler/launcher adapter for cockpit operator actions."""

from pathlib import Path

from kiln.launcher.infrastructure import stop
from kiln.scheduler.application import backlog
from kiln.scheduler.infrastructure.cli import dashboard, retry, send
from kiln.scheduler.infrastructure.persistence import db

from ..application.ports import Session, TaskActionError


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

    def create_task(self, **kwargs) -> dict:
        return self._task_action(backlog.create, **kwargs)

    def update_task(self, **kwargs) -> dict:
        return self._task_action(backlog.update, **kwargs)

    def handoff_task(self, **kwargs) -> dict:
        return self._task_action(backlog.handoff, **kwargs)

    def archive_task(self, **kwargs) -> dict:
        return self._task_action(backlog.archive, **kwargs)

    @staticmethod
    def _task_action(action, **kwargs) -> dict:
        try:
            return action(**kwargs)
        except backlog.BacklogError as exc:
            raise TaskActionError(str(exc)) from exc

    def sequential_enabled(self, *, db_path: Path, branch: str) -> bool:
        return backlog.sequential_enabled(db_path, branch=branch)

    def set_sequential(self, *, db_path: Path, branch: str, enabled: bool) -> None:
        backlog.set_sequential(db_path, branch=branch, enabled=enabled)

    def stop_all(self, roles: list[str]) -> list[int]:
        return stop.stop_all(roles)
