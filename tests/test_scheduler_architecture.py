"""Characterization tests for the scheduler's domain and port boundaries."""

from scheduler import db, policies
from scheduler.models import MessageStatus, can_transition
from scheduler.ports import GitWorktree, SQLiteMessageQueue


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
