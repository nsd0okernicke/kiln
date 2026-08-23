"""Characterization tests for the scheduler's domain and port boundaries."""

from scheduler import db, policies
from scheduler.adapters import WorkerInvocation as LegacyWorkerInvocation
from scheduler.infrastructure import (
    CallableWorkerRunner,
    FileWorkerDebugSink,
    GitWorktree,
    SQLiteMessageQueue,
)
from scheduler.models import MessageStatus, WorkerInvocation, WorkerRequest, can_transition
from scheduler.ports import MessageQueue, WorkerRunner, Worktree
from scheduler.status_contract import STATUS_DONE, WorkerResult


def test_message_lifecycle_rejects_completed_message_reentry(db_path, add_message):
    message_id = add_message(target="coder")
    assert db.mark_processed(db_path, message_id)

    assert db.mark_processing(db_path, message_id) is False
    assert db.get_message(db_path, message_id)["status"] == MessageStatus.PROCESSED


def test_message_lifecycle_centralizes_supported_paths():
    assert can_transition(MessageStatus.QUEUED, MessageStatus.DELIVERED)
    assert can_transition(MessageStatus.PROCESSING, MessageStatus.FAILED)
    assert can_transition(MessageStatus.FAILED, MessageStatus.QUEUED)
    assert not can_transition(MessageStatus.PROCESSED, MessageStatus.PROCESSING)


def test_budget_policy_reports_breach_only_at_the_cap():
    assert policies.budget_breach(spent=4.99, maximum=5.0, work_item="book") == ""
    assert "$5.00 cap" in policies.budget_breach(spent=5.0, maximum=5.0, work_item="book")


def test_cycle_policy_preserves_the_configured_number_of_arrivals():
    assert (
        policies.cycle_limit_breach(arrivals=2, max_cycles=2, work_item="book", role="coder") == ""
    )
    assert "over the limit of 2" in policies.cycle_limit_breach(
        arrivals=3, max_cycles=2, work_item="book", role="coder"
    )


def test_concrete_ports_bind_infrastructure_to_one_context(db_path, git_repo):
    queue = SQLiteMessageQueue(db_path)
    tree = GitWorktree(git_repo)

    assert queue.fetch("coder", "main") is None
    assert tree.head_commit()


def test_concrete_adapters_satisfy_the_application_ports(db_path, git_repo):
    queue: MessageQueue = SQLiteMessageQueue(db_path)
    tree: Worktree = GitWorktree(git_repo)

    assert queue.fetch("coder", "main") is None
    assert tree.persist_inbound("handoff") == git_repo / "tmp" / "handoff-in.md"


def test_worker_runner_translates_the_typed_request_at_the_adapter_edge():
    received = {}

    def legacy_worker(**kwargs):
        received.update(kwargs)
        return WorkerInvocation(WorkerResult(STATUS_DONE, "done", True), "")

    runner: WorkerRunner = CallableWorkerRunner(legacy_worker)
    result = runner(WorkerRequest(prompt="do it", attempt=2, max_budget_usd=3.5))

    assert result.is_done
    assert received == {"prompt": "do it", "attempt": 2, "max_budget_usd": 3.5}


def test_file_debug_sink_owns_diagnostic_persistence(tmp_path):
    sink = FileWorkerDebugSink(tmp_path / "logs")

    sink.save("coder", 2, "raw worker output")

    assert (tmp_path / "logs" / "worker-debug-coder-attempt2.log").read_text(
        encoding="utf-8"
    ) == "raw worker output"


def test_adapter_package_keeps_worker_invocation_compatibility_export():
    assert LegacyWorkerInvocation is WorkerInvocation
