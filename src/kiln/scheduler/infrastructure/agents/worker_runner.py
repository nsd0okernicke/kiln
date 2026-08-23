"""Adapter from provider callables to the application worker-runner port."""

from collections.abc import Callable

from ...domain.models import WorkerInvocation, WorkerRequest


class CallableWorkerRunner:
    def __init__(self, worker: Callable[..., WorkerInvocation]):
        self.worker = worker

    def __call__(self, request: WorkerRequest) -> WorkerInvocation:
        kwargs: dict[str, object] = {"prompt": request.prompt, "attempt": request.attempt}
        if request.max_budget_usd is not None:
            kwargs["max_budget_usd"] = request.max_budget_usd
        return self.worker(**kwargs)
