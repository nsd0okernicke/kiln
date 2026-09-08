"""
`--auto-approve-spec`: the human review gate, stood down for an unattended run.

The forwarded row is the whole contract. Nothing downstream is told a human was skipped, so
it has to look exactly like a real approval to every reader — the coder, the board, the cost
ledger — while still saying, in its own text, that no one reviewed it.

Two processes can start this poller (the human-in-the-loop scheduler and the cockpit, each
behind its own flag), which is why the forward and the acknowledgement have to commit as one
unit: the second thread must find nothing left to do.
"""

from __future__ import annotations

import sqlite3

import pytest

from kiln.scheduler.infrastructure.persistence import db, spec_approval

pytestmark = pytest.mark.integration


@pytest.fixture
def awaiting_review(add_message):
    return add_message(
        sender="specifier",
        target="human-in-the-loop",
        work_item="CAT-3",
        content="Feature: search by author",
    )


def forwarded_to_coder(db_path):
    """Full rows, re-read by id: the activity listing carries only a subset of the columns."""
    return [
        db.get_message(db_path, row["id"])
        for row in db.recent_messages(db_path, "main")
        if row["target"] == "coder"
    ]


class TestForwarding:
    def test_the_scenarios_reach_the_coder_as_an_approval(self, db_path, awaiting_review):
        assert spec_approval.forward_pending_specs(db_path) == ["CAT-3"]

        forwarded = forwarded_to_coder(db_path)
        assert len(forwarded) == 1
        assert forwarded[0]["sender"] == "human-in-the-loop"
        assert forwarded[0]["work_item"] == "CAT-3"
        assert forwarded[0]["status"] == db.STATUS_QUEUED

    def test_the_brief_admits_that_no_human_read_it(self, db_path, awaiting_review):
        # An operator reading the history afterwards has no other way to tell these
        # scenarios through from ones a person actually approved.
        spec_approval.forward_pending_specs(db_path)

        assert "Auto-approved" in forwarded_to_coder(db_path)[0]["content"]

    def test_the_original_is_acknowledged_in_the_same_pass(self, db_path, awaiting_review):
        # Without this the poller hands the coder the same spec every five seconds forever.
        spec_approval.forward_pending_specs(db_path)

        assert db.get_message(db_path, awaiting_review)["acked_at"] is not None

    def test_a_second_pass_finds_nothing_left_to_do(self, db_path, awaiting_review):
        spec_approval.forward_pending_specs(db_path)

        assert spec_approval.forward_pending_specs(db_path) == []
        assert len(forwarded_to_coder(db_path)) == 1

    def test_a_spec_a_human_already_reviewed_is_left_alone(self, db_path, add_message):
        acked = add_message(sender="specifier", target="human-in-the-loop", work_item="CAT-4")
        db.acknowledge_message(db_path, acked, "human-in-the-loop", "main")

        assert spec_approval.forward_pending_specs(db_path) == []

    def test_messages_from_other_roles_are_not_approved(self, db_path, add_message):
        # Only the specifier's Gherkin passes this gate. A reviewer or architect escalation
        # addressed to the human is a question for a person, not a spec to wave on.
        add_message(sender="reviewer", target="human-in-the-loop", work_item="CAT-5")

        assert spec_approval.forward_pending_specs(db_path) == []

    def test_an_unnamed_spec_still_gets_a_grouping_key(self, db_path, add_message):
        # `work_item` becomes the card and the cost key downstream; a NULL there would put
        # the story in no bucket at all.
        add_message(sender="specifier", target="human-in-the-loop", work_item=None)

        assert spec_approval.forward_pending_specs(db_path) == [
            spec_approval.UNNAMED_WORK_ITEM
        ]

    def test_several_waiting_specs_are_forwarded_oldest_first(self, db_path, add_message):
        # The backlog order is the order the specifier produced them in, and the coder works
        # its queue in arrival order.
        for name, created in (("CAT-9", "2026-01-03"), ("CAT-7", "2026-01-01")):
            add_message(
                sender="specifier",
                target="human-in-the-loop",
                work_item=name,
                created_at=f"{created} 00:00:00",
            )

        assert spec_approval.forward_pending_specs(db_path) == ["CAT-7", "CAT-9"]

    def test_a_database_that_is_not_there_is_the_callers_problem(self, tmp_path):
        # One pass raises so the polling loop can decide; a caller invoking it directly
        # should not get a silent no-op that looks like an empty queue.
        with pytest.raises(sqlite3.Error):
            spec_approval.forward_pending_specs(tmp_path / "never-created.db")


# The poller loops forever by design, so these tests stop it by making its own sleep raise.
# That reaches the thread as an unhandled exception, which is the intended signal here.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
class TestPollingLoop:
    @pytest.fixture
    def one_pass(self, monkeypatch):
        """Run the loop exactly once: the second tick leaves the thread instead of sleeping."""

        def _stop(_seconds):
            raise SystemExit

        monkeypatch.setattr(spec_approval, "_sleep", _stop)

        def _run(db_path):
            thread = spec_approval.start_auto_approver(db_path)
            thread.join(timeout=10)
            assert not thread.is_alive()
            return thread

        return _run

    def test_the_loop_forwards_what_is_waiting(self, db_path, one_pass, awaiting_review):
        one_pass(db_path)

        assert len(forwarded_to_coder(db_path)) == 1

    def test_a_database_it_cannot_read_does_not_kill_the_thread(self, tmp_path, one_pass, caplog):
        # It polls a file other processes are writing; a locked or half-created database is
        # an expected transient, and the thread has to still be there on the next tick.
        one_pass(tmp_path / "never-created.db")

        assert "auto-approve" in caplog.text

    def test_it_runs_as_a_daemon_so_it_cannot_hold_the_process_open(self, db_path, one_pass):
        # Nothing ever stops this thread. A non-daemon one would keep the scheduler alive
        # after its work was done, and `kiln --stop` would have to kill it.
        assert one_pass(db_path).daemon
