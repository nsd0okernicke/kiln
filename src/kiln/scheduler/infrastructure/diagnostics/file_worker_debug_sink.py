"""Filesystem implementation of the worker-debug sink port."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class FileWorkerDebugSink:
    def __init__(self, logs_dir: str | Path):
        self.logs_dir = Path(logs_dir)

    def save(self, role: str, attempt: int, raw_output: str) -> None:
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            target = self.logs_dir / f"worker-debug-{role}-attempt{attempt}.log"
            target.write_text(raw_output or "(no output captured)", encoding="utf-8")
            log.info("worker output for attempt %d saved to %s", attempt, target)
        except OSError as exc:
            log.warning("could not save worker debug output: %s", exc)
