"""Worker execution port used by scheduler use cases."""

from typing import Protocol

from ...domain.models import WorkerInvocation, WorkerRequest


class WorkerRunner(Protocol):
    def __call__(self, request: WorkerRequest) -> WorkerInvocation: ...
