"""Diagnostic-output port used by scheduler use cases."""

from typing import Protocol


class WorkerDebugSink(Protocol):
    def save(self, role: str, attempt: int, raw_output: str) -> None: ...
