"""
Sequential mode: what happens the moment a story finishes.

The operator turns this on to run a backlog one story at a time. Everything after the last
role hands off is automatic — push the shared branch, reset every worktree, pull the next
task off the backlog and put it in the intake role's queue — and none of it is what the
cycle was asked to do. So every step is best-effort: a failure here must cost the swarm the
*next* story, never the one it just finished and already recorded.

These are unit tests with hand-built doubles rather than a live swarm, because that
distinction — "logged and swallowed" versus "raised" — is the whole contract, and it is
invisible from the outside when the happy path works.
"""

from __future__ import annotations

import logging

import pytest

from kiln.scheduler.application import process_next_message as scheduler


class _Queue:
    """The four queue methods sequential dispatch touches, and nothing else."""

    def __init__(self, *, sequential=True, backlog=None, arrivals=0):
        self.sequential = sequential
        self.backlog = backlog
        self.arrivals = arrivals
        self.dispatched: list[tuple] = []

    def sequential_enabled(self, branch):
        return self.sequential

    def next_backlog_task(self, branch):
        return self.backlog

    def count_arrivals(self, work_item, branch, target):
        return self.arrivals

    def dispatch_backlog_task(self, work_item, *, branch, sender, target):
        self.dispatched.append((work_item, branch, sender, target))
        return "abcdef1234567890"


class _Worktree:
    def __init__(self):
        self.pushed: list[str] = []
        self.reset: list[str] = []

    def push_branch(self, branch):
        self.pushed.append(branch)

    def reset_all_worktrees(self, branch):
        self.reset.append(branch)


class _Routing:
    def __init__(self, intake="specifier"):
        self.intake = intake

    def resolve(self, role):
        return self.intake


class _Exploding:
    """Every attribute access is a call that fails — the store being down, in one object."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise RuntimeError(f"{name} unavailable")

        return _raise


def make_ctx(queue=None, worktree=None, routing=None, role="reviewer"):
    ctx = object.__new__(scheduler.SchedulerContext)
    object.__setattr__(ctx, "queue", queue if queue is not None else _Queue())
    object.__setattr__(ctx, "worktree_port", worktree if worktree is not None else _Worktree())
    object.__setattr__(ctx, "routing", routing if routing is not None else _Routing())
    object.__setattr__(ctx, "role", role)
    object.__setattr__(ctx, "branch", "main")
    return ctx


TASK = {"work_item": "CAT-4", "title": "Search by ISBN", "body": "..."}


class TestAutoDispatch:
    def test_the_next_backlog_task_goes_to_the_intake_role(self):
        queue = _Queue(backlog=TASK)

        scheduler._auto_dispatch_next(make_ctx(queue))

        assert queue.dispatched == [("CAT-4", "main", "human-in-the-loop", "specifier")]

    def test_nothing_happens_when_sequential_mode_is_off(self):
        # The default. Dispatching here would start a second story while the operator is
        # still working through the first by hand.
        queue = _Queue(sequential=False, backlog=TASK)
        worktree = _Worktree()

        scheduler._auto_dispatch_next(make_ctx(queue, worktree))

        assert queue.dispatched == [] and worktree.pushed == []

    def test_an_empty_backlog_ends_the_run_quietly(self):
        queue = _Queue(backlog=None)

        scheduler._auto_dispatch_next(make_ctx(queue))

        assert queue.dispatched == []

    def test_a_story_that_already_reached_the_human_is_not_started_again(self):
        # The backlog row can still be there when the work is done; arrivals at the human
        # queue are the record of completion, and re-dispatching would loop the swarm.
        queue = _Queue(backlog=TASK, arrivals=1)

        scheduler._auto_dispatch_next(make_ctx(queue))

        assert queue.dispatched == []

    def test_the_shared_branch_is_pushed_before_the_next_story_starts(self):
        # The next story is briefed against the shared branch. Dispatching before the push
        # would hand the specifier a tree without the work that just finished.
        worktree = _Worktree()

        scheduler._auto_dispatch_next(make_ctx(_Queue(backlog=TASK), worktree))

        assert worktree.pushed == ["main"] and worktree.reset == ["main"]

    def test_a_push_that_fails_does_not_stop_the_dispatch(self, caplog):
        # A push can fail for reasons that have nothing to do with the backlog, and the
        # operator asked for the next story.
        queue = _Queue(backlog=TASK)

        with caplog.at_level(logging.DEBUG, logger=scheduler.log.name):
            scheduler._auto_dispatch_next(make_ctx(queue, _Exploding()))

        assert queue.dispatched != []

    def test_a_queue_that_is_down_is_swallowed_rather_than_failing_the_cycle(self):
        # The cycle that triggered this has already been recorded as done. Raising here
        # would report the finished story as a failure.
        scheduler._auto_dispatch_next(make_ctx(_Exploding()))

    def test_a_dispatch_that_fails_is_reported_and_left(self, caplog):
        class _Refusing(_Queue):
            def dispatch_backlog_task(self, *args, **kwargs):
                raise RuntimeError("queue is locked")

        with caplog.at_level(logging.WARNING, logger=scheduler.log.name):
            scheduler._auto_dispatch_next(make_ctx(_Refusing(backlog=TASK)))

        assert "CAT-4" in caplog.text

    def test_a_swarm_with_no_intake_role_says_so_instead_of_guessing_one(self, caplog):
        # A profile that routes nothing to the human has nowhere to put the next story;
        # picking a role would put work in a queue the operator never chose.
        queue = _Queue(backlog=TASK)

        with caplog.at_level(logging.WARNING, logger=scheduler.log.name):
            scheduler._auto_dispatch_next(make_ctx(queue, routing=_Routing(intake=None)))

        assert queue.dispatched == []
        assert "no intake role" in caplog.text


class TestSpecDefectTask:
    """
    A specifier whose own gate fails leaves a backlog task behind.

    The escalation goes to the human either way; this is the part that survives it. Only
    failures that are actually about the specification qualify — a flaky container or a
    network timeout is not a defect in the scenarios and must not become a story.
    """

    class _Recording:
        def __init__(self, task=None):
            self.created: list[dict] = []
            self.task = task if task is not None else {"id": "1"}

        def create_spec_defect_task(self, *, branch, work_item, failure_detail):
            self.created.append(
                {"branch": branch, "work_item": work_item, "detail": failure_detail}
            )
            return self.task

    @pytest.mark.parametrize("word", ["gherkin", "spec", "scenario", "feature"])
    def test_a_failure_about_the_specification_becomes_a_task(self, word):
        queue = self._Recording()

        scheduler._auto_create_spec_defect(make_ctx(queue), f"the {word} did not parse")

        assert len(queue.created) == 1
        assert queue.created[0]["work_item"].startswith("spec-defect-")

    def test_the_match_is_case_insensitive(self):
        queue = self._Recording()

        scheduler._auto_create_spec_defect(make_ctx(queue), "Scenario Outline is malformed")

        assert len(queue.created) == 1

    def test_an_unrelated_failure_does_not_become_a_story(self):
        queue = self._Recording()

        scheduler._auto_create_spec_defect(make_ctx(queue), "postgres container never started")

        assert queue.created == []

    def test_the_failure_output_travels_with_the_task(self):
        # Whoever picks the task up needs to see what actually failed; the work item name
        # is only a timestamp.
        queue = self._Recording()

        scheduler._auto_create_spec_defect(make_ctx(queue), "scenario 2 asserts no ordering")

        assert "scenario 2 asserts no ordering" in queue.created[0]["detail"]

    def test_a_very_long_failure_is_truncated_rather_than_stored_whole(self):
        # It becomes a backlog card the operator reads on screen, not a log file.
        queue = self._Recording()

        scheduler._auto_create_spec_defect(make_ctx(queue), "scenario " + "x" * 5000)

        assert len(queue.created[0]["detail"]) == 2000

    def test_a_store_that_refuses_the_task_does_not_fail_the_escalation(self, caplog):
        # The escalation to the human is the part that matters; this is a convenience on
        # top of it and must not be able to take it down.
        with caplog.at_level(logging.WARNING, logger=scheduler.log.name):
            scheduler._auto_create_spec_defect(make_ctx(_Exploding()), "scenario broke")

        assert "could not auto-create spec-defect task" in caplog.text
