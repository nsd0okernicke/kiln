"""
The scheduler cycle, end to end: real SQLite, real git, fake worker.

The fake worker is what makes this tier possible — every branch of the loop (merge
failure, retry, escalation, circuit breaker, ping) is exercised deterministically with no
LLM cost and no flakiness. Only the worker subprocess itself is substituted; the queue,
the routing table, the git history and the message formats are all the real ones.
"""

from __future__ import annotations

import itertools
import subprocess
from datetime import datetime

import pytest
from kiln.scheduler.domain import handoff
from kiln.scheduler.domain.models import TokenUsage
from kiln.scheduler.domain.routing import parse_routing_table
from kiln.scheduler.domain.status_contract import STATUS_BLOCKED, STATUS_DONE, WorkerResult
from kiln.scheduler.domain.worker_prompt import WorkerDefinition
from kiln.scheduler.infrastructure.agents.claude_adapter import WorkerInvocation
from kiln.scheduler.infrastructure.agents.worker_runner import CallableWorkerRunner
from kiln.scheduler.infrastructure.cli import role_scheduler
from kiln.scheduler.infrastructure.cli.role_scheduler import (
    CycleResult,
    SchedulerContext,
    SchedulerState,
)
from kiln.scheduler.infrastructure.diagnostics import FileWorkerDebugSink
from kiln.scheduler.infrastructure.diagnostics import verification as verify
from kiln.scheduler.infrastructure.persistence import SQLiteMessageQueue, db
from kiln.scheduler.infrastructure.vcs import GitWorktree
from kiln.scheduler.infrastructure.vcs import git as git_ops

pytestmark = pytest.mark.integration

ROUTING = parse_routing_table(
    """
## Handoff Routing

| Role | Sends to | When Sender |
| ---- | -------- | ----------- |
| specifier | coder | |
| specifier | human-in-the-loop | architect |
| coder | refactorer | |
| refactorer | architect | |
| architect | specifier | |
"""
)

DEFINITION = WorkerDefinition(name="coder-worker", description="d", prompt="body")

FIXED_NOW = datetime(2026, 8, 7, 14, 3, 11)


def worker(status=STATUS_DONE, summary="did the work", handoff_name="", **kwargs):
    """
    Build a canned worker outcome.

    `handoff_name` is what a real adapter would have got from `parse_worker_report` reading a
    `KILN-HANDOFF:` line; the parsing itself is covered in test_status_contract.py, so these
    tests set the parsed field directly rather than round-tripping through stdout.
    """
    kwargs.setdefault("raw_output", f"KILN-STATUS: {status} {summary}")
    return WorkerInvocation(
        result=WorkerResult(
            status=status, summary=summary, sentinel_found=True, handoff_name=handoff_name
        ),
        **kwargs,
    )


class FakeWorker:
    """
    Returns queued outcomes in order, recording the prompts it was given.

    Writes a file by default, because a worker that reports `done` having changed nothing is
    now the *exceptional* case: the scheduler treats it as a terminated chain and hands off
    nothing (`NO_OP`). Pass `produces_work=False` to exercise that path deliberately; pass an
    explicit `edits_file` when the test cares which file was touched.
    """

    def __init__(self, *outcomes, edits_file=None, produces_work=True):
        self.outcomes = list(outcomes)
        self.prompts = []
        self.budgets = []
        self.edits_file = edits_file
        self.produces_work = produces_work

    def bind_worktree(self, worktree):
        """Give an unconfigured worker somewhere to write, once the worktree is known."""
        if self.produces_work and self.edits_file is None:
            self.edits_file = worktree / "worker-output.txt"

    def __call__(self, *, prompt, attempt=1, **kwargs):
        # kwargs, not a named parameter: the scheduler passes `max_budget_usd` only when a
        # cap is configured, and `budgets` records exactly what a real adapter would see.
        self.prompts.append(prompt)
        self.budgets.append(kwargs.get("max_budget_usd"))
        if self.edits_file:
            self.edits_file.write_text(f"worker output {len(self.prompts)}\n", encoding="utf-8")
        return self.outcomes.pop(0) if self.outcomes else worker()

    @property
    def calls(self):
        return len(self.prompts)


@pytest.fixture
def make_ctx(db_path, git_repo):
    def _make(run_worker, role="coder", **overrides):
        if isinstance(run_worker, FakeWorker):
            run_worker.bind_worktree(overrides.get("worktree", git_repo))
        args = {
            "role": role,
            "branch": "main",
            "worktree": git_repo,
            "routing": ROUTING,
            "definition": DEFINITION,
            "worker_runner": CallableWorkerRunner(run_worker),
            "queue": SQLiteMessageQueue(db_path),
            "worktree_port": GitWorktree(overrides.get("worktree", git_repo)),
            "debug_sink": FileWorkerDebugSink(db_path.parent / "logs"),
            "queue_label": str(db_path),
            "clock": lambda: FIXED_NOW,
        }
        args.update(overrides)
        return SchedulerContext(**args)

    return _make


@pytest.fixture
def inbound(db_path, git_repo, git_cmd):
    """Queue a realistic handoff whose commit actually exists on a sender branch."""

    counter = itertools.count(1)

    def _queue(*, target="coder", sender="specifier", ping=False, commit=None, name="order-intake"):
        if commit is None:
            # Unique branch/file names so the fixture can be called repeatedly within one
            # test (the circuit-breaker cases need three cycles).
            nth = next(counter)
            git_cmd(git_repo, "checkout", "-q", "-b", f"sender-{sender}-{nth}")
            (git_repo / f"{sender}-{nth}.txt").write_text("sender work\n", encoding="utf-8")
            git_cmd(git_repo, "add", "-A")
            git_cmd(git_repo, "commit", "-qm", "sender work")
            commit = git_ops.head_commit(git_repo)
            git_cmd(git_repo, "checkout", "-q", "main")
            (git_repo / f"own-{nth}.txt").write_text("own\n", encoding="utf-8")
            git_cmd(git_repo, "add", "-A")
            git_cmd(git_repo, "commit", "-qm", "own work")

        content = handoff.format_handoff(
            sender=sender,
            handoff=name,
            branch="main",
            commit=commit,
            summary="Please do your part.",
            next_role=target,
            timestamp="2026-08-07 13:00:00",
            ping=ping,
            trail=("human-in-the-loop (main)",) if ping else (),
        )
        return db.insert_handoff(db_path, sender, target, content, "main"), commit

    return _queue


def queued_for(db_path, target):
    """All queued messages addressed to a role."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE target=? AND status='queued' ORDER BY created_at",
            (target,),
        ).fetchall()
    return [dict(r) for r in rows]


class TestIdle:
    def test_empty_inbox_is_idle(self, make_ctx):
        fake = FakeWorker()
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert result.outcome == role_scheduler.IDLE
        assert fake.calls == 0, "an empty inbox must never invoke a worker"


class TestHappyPath:
    def test_full_cycle(self, make_ctx, inbound, db_path, read_message):
        message_id, _ = inbound()
        fake = FakeWorker(worker(summary="implemented order creation", cost_usd=0.05))

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.HANDED_OFF
        assert result.target == "refactorer"
        assert fake.calls == 1
        assert read_message(message_id)["status"] == db.STATUS_PROCESSED

    def test_hands_off_exactly_one_message_to_the_routed_target(self, make_ctx, inbound, db_path):
        inbound()
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert len(queued_for(db_path, "refactorer")) == 1
        assert queued_for(db_path, "architect") == []

    def test_outbound_message_is_parseable_and_carries_the_handoff_name(
        self, make_ctx, inbound, db_path
    ):
        inbound(name="order-intake")
        role_scheduler.run_once(make_ctx(FakeWorker(worker(summary="done it"))), SchedulerState())

        parsed = handoff.parse_handoff(queued_for(db_path, "refactorer")[0]["content"])
        assert parsed.sender == "coder"
        assert parsed.handoff == "order-intake", "the specifier's handoff name must survive"
        assert parsed.branch == "main"
        assert parsed.commit
        assert "done it" in queued_for(db_path, "refactorer")[0]["content"]

    def test_the_work_item_column_carries_the_handoff_name(self, make_ctx, inbound, db_path):
        # The name used to live only as prose inside `content` -- parsed, then dropped.
        # As a column it can be grouped and counted, which is what per-feature cost and
        # loop detection need.
        inbound(name="order-intake")
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert queued_for(db_path, "refactorer")[0]["work_item"] == "order-intake"

    def test_the_stored_work_item_matches_the_name_in_the_message(self, make_ctx, inbound, db_path):
        # Two spellings of the same work item would split the group silently.
        inbound(name="CAT-3 search by author")
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        row = queued_for(db_path, "refactorer")[0]
        assert row["work_item"] == handoff.parse_handoff(row["content"]).handoff

    def test_creates_one_role_prefixed_squash_commit(self, make_ctx, inbound, git_repo):
        inbound()
        fake = FakeWorker(worker(summary="implemented it"), edits_file=git_repo / "feature.py")
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        # Everything after the cycle's own merge commit must be exactly one squash commit.
        anchor = git_ops.squash_anchor(git_repo)
        subjects = git_ops.run_git(
            ["log", "--format=%s", f"{anchor}..HEAD"], git_repo
        ).stdout.splitlines()
        assert subjects == ["[Coder] implemented it"]

    def test_commits_work_the_worker_left_uncommitted(self, make_ctx, inbound, git_repo):
        inbound()
        fake = FakeWorker(worker(), edits_file=git_repo / "feature.py")
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert git_ops.has_pending_changes(git_repo) is False
        assert (git_repo / "feature.py").read_text(encoding="utf-8") == "worker output 1\n"

    def test_merges_the_senders_commit(self, make_ctx, inbound, git_repo):
        inbound(sender="specifier")
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert (git_repo / "specifier-1.txt").exists(), "sender's work must be merged in"

    def test_merge_commit_carries_role_and_sender_not_gits_generic_default(
        self, make_ctx, inbound, git_repo
    ):
        inbound(sender="specifier")
        role_scheduler.run_once(make_ctx(FakeWorker(), role="coder"), SchedulerState())
        subjects = git_ops.run_git(["log", "--format=%s"], git_repo).stdout.splitlines()
        assert any(s.startswith("[Coder] Merge order-intake from specifier") for s in subjects)
        assert not any(s.startswith("Merge commit '") for s in subjects)

    def test_writes_the_inbound_message_for_debugging(self, make_ctx, inbound, git_repo):
        inbound()
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert "Sender: specifier" in (git_repo / "tmp" / "handoff-in.md").read_text("utf-8")

    def test_worker_receives_the_handoff_verbatim(self, make_ctx, inbound):
        inbound()
        fake = FakeWorker()
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert "Sender: specifier" in fake.prompts[0]
        assert "Handoff: order-intake" in fake.prompts[0]


class TestConditionalRouting:
    def test_specifier_forwards_architect_reports_to_the_human(self, make_ctx, inbound, db_path):
        # The override that was previously prose only an LLM could follow.
        inbound(target="specifier", sender="architect")
        result = role_scheduler.run_once(make_ctx(FakeWorker(), role="specifier"), SchedulerState())
        assert result.target == "human-in-the-loop"
        assert len(queued_for(db_path, "human-in-the-loop")) == 1

    def test_specifier_sends_normal_requests_to_the_coder(self, make_ctx, inbound):
        inbound(target="specifier", sender="human-in-the-loop")
        result = role_scheduler.run_once(make_ctx(FakeWorker(), role="specifier"), SchedulerState())
        assert result.target == "coder"

    def test_unroutable_role_escalates_instead_of_guessing(self, make_ctx, inbound, db_path):
        inbound(target="nobody", sender="specifier")
        result = role_scheduler.run_once(make_ctx(FakeWorker(), role="nobody"), SchedulerState())
        assert result.outcome == role_scheduler.NO_ROUTE
        assert len(queued_for(db_path, "human-in-the-loop")) == 1


class TestRetryPolicy:
    def test_blocked_once_then_done_hands_off(self, make_ctx, inbound, db_path):
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "missing fixtures"), worker(summary="fixed"))

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.HANDED_OFF
        assert fake.calls == 2
        assert len(queued_for(db_path, "refactorer")) == 1

    def test_retry_prompt_includes_the_previous_failure(self, make_ctx, inbound):
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "missing fixtures"), worker())
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert "missing fixtures" in fake.prompts[1]
        assert "Previous attempt failed" in fake.prompts[1]

    def test_blocked_twice_escalates_and_suppresses_the_handoff(self, make_ctx, inbound, db_path):
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "still no"))

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.ESCALATED
        assert fake.calls == 2, "must not retry more than once"
        assert queued_for(db_path, "refactorer") == [], "blocked work must never be forwarded"
        assert len(queued_for(db_path, "human-in-the-loop")) == 1

    def test_escalation_message_is_marked_and_still_parseable(self, make_ctx, inbound, db_path):
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "still no"))
        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        content = queued_for(db_path, "human-in-the-loop")[0]["content"]
        assert "Kiln-Escalation: true" in content
        assert "still no" in content
        assert handoff.parse_handoff(content).sender == "coder"

    def test_an_escalation_stays_attached_to_its_work_item(self, make_ctx, inbound, db_path):
        # An escalation that lost the work item could not be counted against it, and a
        # future resume would have nothing to re-attach to.
        inbound(name="order-intake")
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "still no"))
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert queued_for(db_path, "human-in-the-loop")[0]["work_item"] == "order-intake"

    def test_inbound_message_never_wedges_in_processing(self, make_ctx, inbound, read_message):
        # The point is the *absence* of `processing`, which is what would strand the message.
        # It lands on `failed` rather than `processed` now: it did not complete, and marking
        # it processed made a failed cycle indistinguishable from a successful one, leaving
        # `kiln retry` nothing to address.
        message_id, _ = inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert read_message(message_id)["status"] == db.STATUS_FAILED


class TestWorkerDebugPersistence:
    """
    A blocked WorkerInvocation's raw_output must survive the process, not just its one-line
    summary. Found live: a copilot worker's summary said "0 stream events seen" while its own
    resumed session proved substantial activity happened -- with no persisted raw output,
    there was no way to tell whether nothing was captured or the capture itself was lying.
    """

    def _debug_file(self, db_path, role, attempt):
        return db_path.parent / "logs" / f"worker-debug-{role}-attempt{attempt}.log"

    def test_a_blocked_attempts_raw_output_is_saved(self, make_ctx, inbound, db_path):
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", raw_output="the actual raw stream"),
            worker(),
        )
        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        saved = self._debug_file(db_path, "coder", 1)
        assert saved.is_file()
        assert saved.read_text(encoding="utf-8") == "the actual raw stream"

    def test_each_retry_attempt_gets_its_own_file(self, make_ctx, inbound, db_path):
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", raw_output="attempt one"),
            worker(STATUS_BLOCKED, "still no", raw_output="attempt two"),
        )
        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert self._debug_file(db_path, "coder", 1).read_text(encoding="utf-8") == "attempt one"
        assert self._debug_file(db_path, "coder", 2).read_text(encoding="utf-8") == "attempt two"

    def test_empty_raw_output_still_leaves_evidence(self, make_ctx, inbound, db_path):
        # An empty capture is itself the diagnostic: it says nothing reached the adapter.
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no", raw_output=""), worker())
        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert "no output captured" in self._debug_file(db_path, "coder", 1).read_text("utf-8")

    def test_a_successful_attempt_leaves_no_debug_file(self, make_ctx, inbound, db_path):
        inbound()
        role_scheduler.run_once(make_ctx(FakeWorker(worker())), SchedulerState())
        assert not self._debug_file(db_path, "coder", 1).exists()


class TestRetryDecision:
    """The policy as a pure function, independent of DB, git and workers."""

    def test_no_retry_after_success(self):
        assert role_scheduler.should_retry([worker()], max_attempts=2) is False

    def test_retry_after_first_failure(self):
        assert role_scheduler.should_retry([worker(STATUS_BLOCKED)], max_attempts=2) is True

    def test_no_retry_once_the_budget_is_spent(self):
        attempts = [worker(STATUS_BLOCKED), worker(STATUS_BLOCKED)]
        assert role_scheduler.should_retry(attempts, max_attempts=2) is False

    def test_no_retry_with_no_attempts(self):
        assert role_scheduler.should_retry([], max_attempts=2) is False

    def test_honours_a_raised_budget(self):
        attempts = [worker(STATUS_BLOCKED), worker(STATUS_BLOCKED)]
        assert role_scheduler.should_retry(attempts, max_attempts=3) is True


class TestMergeFailure:
    def test_conflict_escalates_without_delegating(self, make_ctx, inbound, git_repo, git_cmd):
        # Set up a genuine conflict between the sender's commit and this worktree.
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        (git_repo / "shared.txt").write_text("sender version\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "sender edit")
        conflicting = git_ops.head_commit(git_repo)
        git_cmd(git_repo, "checkout", "-q", "main")
        (git_repo / "shared.txt").write_text("main version\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "main edit")

        inbound(commit=conflicting)
        fake = FakeWorker()

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.MERGE_FAILED
        assert fake.calls == 0, "must not delegate on top of a failed merge"

    def test_conflict_leaves_a_clean_tree(self, make_ctx, inbound, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        (git_repo / "shared.txt").write_text("sender\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "sender edit")
        conflicting = git_ops.head_commit(git_repo)
        git_cmd(git_repo, "checkout", "-q", "main")
        (git_repo / "shared.txt").write_text("main\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "main edit")

        inbound(commit=conflicting)
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())

        assert git_ops.has_pending_changes(git_repo) is False


class TestPing:
    def test_forwards_without_delegating(self, make_ctx, inbound, db_path):
        inbound(ping=True)
        fake = FakeWorker()

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.PING_FORWARDED
        assert fake.calls == 0, "a health check must not run the role's real work"
        assert result.target == "refactorer"

    def test_appends_this_role_to_the_trail(self, make_ctx, inbound, db_path):
        inbound(ping=True)
        role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())

        parsed = handoff.parse_handoff(queued_for(db_path, "refactorer")[0]["content"])
        assert parsed.is_ping is True
        assert parsed.trail == ("human-in-the-loop (main)", "coder (main)")


class TestNoOpCycle:
    """
    `roles/architect.md`: "Do not hand off changes if the handoff contains no changes." The
    scheduler did not honour it -- `squash_since` treats an empty range as success and returns
    the current HEAD, so the role forwarded a commit containing none of its own work and the
    swarm kept spending on a cycle that had already concluded.
    """

    def _no_op_cycle(self, make_ctx, inbound, **kwargs):
        inbound()
        fake = FakeWorker(worker(summary="nothing to change"), produces_work=False)
        return role_scheduler.run_once(make_ctx(fake, **kwargs), SchedulerState())

    def test_a_cycle_that_changes_nothing_forwards_nothing(self, make_ctx, inbound, db_path):
        result = self._no_op_cycle(make_ctx, inbound)

        assert result.outcome == role_scheduler.NO_OP
        assert queued_for(db_path, "refactorer") == [], "the chain must stop, not continue"

    def test_the_human_is_told_the_chain_ended(self, make_ctx, inbound, db_path):
        # A swarm that simply goes quiet is indistinguishable from one that died.
        self._no_op_cycle(make_ctx, inbound)

        messages = queued_for(db_path, "human-in-the-loop")
        assert len(messages) == 1
        assert "reviewed the inbound handoff" in messages[0]["content"]
        assert "produced no additional changes" in messages[0]["content"]

    def test_that_message_is_informational_not_an_escalation(self, make_ctx, inbound, db_path):
        # The run concluded correctly; it just concluded. Flagging it as an escalation would
        # put a non-problem in front of a human at the same weight as a blocked worker.
        self._no_op_cycle(make_ctx, inbound)

        message = queued_for(db_path, "human-in-the-loop")[0]
        assert message["priority"] >= role_scheduler.INFORMATIONAL_PRIORITY
        assert handoff.is_escalation(message["content"]) is False

    def test_the_inbound_message_is_still_marked_processed(self, make_ctx, inbound, read_message):
        # Otherwise it sits in `processing` forever and startup recovery replays it.
        result = self._no_op_cycle(make_ctx, inbound)
        assert read_message(result.message_id)["status"] == db.STATUS_PROCESSED

    def test_it_does_not_trip_the_circuit_breaker(self, make_ctx, inbound):
        # Not a failure to count -- matching _forward_ping, which also leaves it alone.
        inbound()
        state = SchedulerState()
        fake = FakeWorker(worker(), produces_work=False)

        role_scheduler.run_once(make_ctx(fake), state)

        assert state.consecutive_escalations == 0
        assert state.halted is False

    def test_a_blocked_worker_still_escalates_rather_than_no_ops(self, make_ctx, inbound):
        # Both leave the worktree untouched. Only one of them means "there was nothing to do".
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"), produces_work=False
        )

        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.ESCALATED

    def test_a_ping_is_unaffected(self, make_ctx, inbound, db_path):
        # Pings legitimately change nothing, and route through _forward_ping long before the
        # no-op check. If this ever regressed, /kiln-ping would stop reaching the far end.
        inbound(ping=True)
        result = role_scheduler.run_once(
            make_ctx(FakeWorker(produces_work=False)), SchedulerState()
        )
        assert result.outcome == role_scheduler.PING_FORWARDED
        assert len(queued_for(db_path, "refactorer")) == 1


class TestMaxCyclesPerWorkItem:
    """
    The existing stop conditions all catch *failure*. None catches expensive success, so
    spec<->code ping-pong ran until a human noticed. A ceiling on how many times one work
    item may reach a role gives that a bound.
    """

    def _arrive(self, db_path, name, times, target="coder"):
        """
        Earlier laps of one work item, left exactly as finished laps look: processed.

        Processed rather than queued deliberately -- a queued leftover would be picked up by
        `fetch_and_deliver` ahead of the message under test, and the count has to include
        laps that are already done or a swarm could loop forever without ever tripping it.
        """
        for _ in range(times):
            message_id = db.insert_handoff(
                db_path, "specifier", target, "old", "main", work_item=name
            )
            db.mark_processed(db_path, message_id)

    def test_under_the_limit_the_cycle_runs_normally(self, make_ctx, inbound, db_path):
        inbound(name="loopy")
        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_cycles=5), SchedulerState())
        assert result.outcome == role_scheduler.HANDED_OFF

    def test_over_the_limit_it_escalates_instead(self, make_ctx, inbound, db_path):
        self._arrive(db_path, "loopy", 3)
        inbound(name="loopy")

        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_cycles=2), SchedulerState())

        assert result.outcome == role_scheduler.MAX_CYCLES
        assert result.target == "human-in-the-loop"

    def test_it_stops_before_paying_for_another_worker_run(self, make_ctx, inbound, db_path):
        # The whole point is not spending. Checking after delegation would bound the count
        # but not the bill.
        self._arrive(db_path, "loopy", 3)
        inbound(name="loopy")
        fake = FakeWorker()

        role_scheduler.run_once(make_ctx(fake, max_cycles=2), SchedulerState())

        assert fake.calls == 0

    def test_the_escalation_names_the_work_item_and_the_count(self, make_ctx, inbound, db_path):
        self._arrive(db_path, "loopy", 3)
        inbound(name="loopy")

        role_scheduler.run_once(make_ctx(FakeWorker(), max_cycles=2), SchedulerState())

        message = queued_for(db_path, "human-in-the-loop")[0]["content"]
        assert "loopy" in message and "limit of 2" in message

    def test_another_work_item_is_counted_separately(self, make_ctx, inbound, db_path):
        # A shared counter would let one busy feature stop an unrelated one.
        self._arrive(db_path, "loopy", 9)
        inbound(name="innocent")

        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_cycles=2), SchedulerState())

        assert result.outcome == role_scheduler.HANDED_OFF

    def test_another_role_is_counted_separately(self, make_ctx, inbound, db_path):
        # Counting every message for the item would mix lap *length* into the number, so the
        # same ceiling would mean different things in a 4-role profile and a 2-role one.
        self._arrive(db_path, "loopy", 9, target="refactorer")
        inbound(name="loopy")

        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_cycles=2), SchedulerState())

        assert result.outcome == role_scheduler.HANDED_OFF

    def test_no_limit_is_the_default(self, make_ctx, inbound, db_path):
        self._arrive(db_path, "loopy", 50)
        inbound(name="loopy")
        result = role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert result.outcome == role_scheduler.HANDED_OFF


class TestCostCap:
    def _spent(self, state, work_item, amount):
        state.record_spend(work_item, amount)
        return state

    def test_under_the_cap_the_cycle_runs(self, make_ctx, inbound):
        inbound(name="pricey")
        state = self._spent(SchedulerState(), "pricey", 1.0)

        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_budget_usd=5.0), state)

        assert result.outcome == role_scheduler.HANDED_OFF

    def test_at_the_cap_it_escalates(self, make_ctx, inbound):
        inbound(name="pricey")
        state = self._spent(SchedulerState(), "pricey", 5.0)

        result = role_scheduler.run_once(make_ctx(FakeWorker(), max_budget_usd=5.0), state)

        assert result.outcome == role_scheduler.COST_CAP

    def test_it_stops_before_spending_more(self, make_ctx, inbound):
        inbound(name="pricey")
        fake = FakeWorker()
        state = self._spent(SchedulerState(), "pricey", 5.0)

        role_scheduler.run_once(make_ctx(fake, max_budget_usd=5.0), state)

        assert fake.calls == 0

    def test_spend_accumulates_across_cycles(self, make_ctx, inbound):
        # The tally is what the cap reads; a cycle that does not add to it makes the cap
        # unreachable no matter how much the swarm spends.
        inbound(name="pricey")
        state = SchedulerState()

        role_scheduler.run_once(
            make_ctx(FakeWorker(worker(cost_usd=2.5)), max_budget_usd=99.0), state
        )

        assert state.spend_on("pricey") == pytest.approx(2.5)

    def test_retries_are_charged_too(self, make_ctx, inbound):
        # A retried cycle costs the sum of its attempts. Counting only the last one would
        # make the expensive cycles look like the cheap ones.
        inbound(name="pricey")
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", cost_usd=1.0), worker(summary="ok", cost_usd=2.0)
        )
        state = SchedulerState()

        role_scheduler.run_once(make_ctx(fake, max_budget_usd=99.0), state)

        assert state.spend_on("pricey") == pytest.approx(3.0)

    def test_the_worker_is_given_the_remaining_budget_not_the_whole_cap(self, make_ctx, inbound):
        # A retry after a $4 first attempt under a $5 cap must not be handed $5 again.
        inbound(name="pricey")
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", cost_usd=4.0), worker(summary="ok", cost_usd=0.5)
        )

        role_scheduler.run_once(make_ctx(fake, max_budget_usd=5.0), SchedulerState())

        assert fake.budgets == [pytest.approx(5.0), pytest.approx(1.0)]

    def test_the_remaining_budget_never_goes_negative(self, make_ctx, inbound):
        # An overspending first attempt would otherwise hand the CLI a negative ceiling.
        inbound(name="pricey")
        fake = FakeWorker(worker(STATUS_BLOCKED, "no", cost_usd=9.0), worker(summary="ok"))

        role_scheduler.run_once(make_ctx(fake, max_budget_usd=5.0), SchedulerState())

        assert fake.budgets[1] == 0.0

    def test_no_cap_means_the_worker_is_told_nothing(self, make_ctx, inbound):
        # Passed only when configured, so adapters without the flag are unaffected.
        inbound(name="pricey")
        fake = FakeWorker()
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert fake.budgets == [None]


class TestCircuitBreaker:
    def _fail_once(self, ctx_factory, inbound, state):
        inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
        return role_scheduler.run_once(ctx_factory(fake), state)

    def test_halts_after_three_consecutive_escalations(self, make_ctx, inbound):
        state = SchedulerState()
        for _ in range(3):
            self._fail_once(make_ctx, inbound, state)

        assert state.halted is True
        assert state.consecutive_escalations == 3

    def test_halted_scheduler_stops_consuming_messages(self, make_ctx, inbound, db_path):
        state = SchedulerState(halted=True)
        message_id, _ = inbound()

        result = role_scheduler.run_once(make_ctx(FakeWorker()), state)

        assert result.outcome == role_scheduler.HALTED
        assert queued_for(db_path, "coder")[0]["id"] == message_id, "message must be left queued"

    def test_announces_the_halt_to_the_human(self, make_ctx, inbound, db_path):
        state = SchedulerState()
        for _ in range(3):
            self._fail_once(make_ctx, inbound, state)

        contents = [m["content"] for m in queued_for(db_path, "human-in-the-loop")]
        assert any("CIRCUIT BREAKER" in c for c in contents)

    def test_a_successful_cycle_rearms_the_breaker(self, make_ctx, inbound):
        state = SchedulerState()
        self._fail_once(make_ctx, inbound, state)
        self._fail_once(make_ctx, inbound, state)
        assert state.consecutive_escalations == 2

        inbound()
        role_scheduler.run_once(make_ctx(FakeWorker(worker())), state)

        assert state.consecutive_escalations == 0
        assert state.halted is False


class TestNamingTheWorkItem:
    """
    Found live, after two clean cycles in a real project: **every** row in the queue had
    `work_item = 'pending'`.

    In scheduler mode the scheduler composes the outbound message, copying `Handoff:` from the
    inbound verbatim, and the worker's only channel was the status sentinel -- so
    `roles/specifier.md`'s instruction to "invent the handoff name, replacing the `pending`
    placeholder" could not be carried out by anything. The placeholder propagated through every
    hop of every cycle, and `count_work_item_arrivals` and `spend_by_work_item` -- the max-cycles
    guard and the cost cap -- were both counting across unrelated features as a result.
    """

    def test_the_specifier_can_name_a_pending_work_item(self, make_ctx, inbound, db_path):
        inbound(target="specifier", sender="human-in-the-loop", name="pending")
        fake = FakeWorker(worker(summary="wrote the spec", handoff_name="cat-3-search-by-author"))

        role_scheduler.run_once(make_ctx(fake, role="specifier"), SchedulerState())

        row = queued_for(db_path, "coder")[0]
        assert row["work_item"] == "cat-3-search-by-author"

    def test_the_message_header_carries_the_same_name_as_the_column(
        self, make_ctx, inbound, db_path
    ):
        # The column is only trustworthy if it matches what a human reads in the message.
        inbound(target="specifier", sender="human-in-the-loop", name="pending")
        fake = FakeWorker(worker(handoff_name="cat-3"))

        role_scheduler.run_once(make_ctx(fake, role="specifier"), SchedulerState())

        row = queued_for(db_path, "coder")[0]
        assert handoff.parse_handoff(row["content"]).handoff == row["work_item"] == "cat-3"

    def test_a_later_role_cannot_rename_the_work(self, make_ctx, inbound, db_path):
        # The one restriction that makes a work item an identity rather than a label: whoever
        # accepts the request names it, and everyone after carries that name unchanged.
        inbound(name="cat-3-search")
        fake = FakeWorker(worker(handoff_name="something-else"))

        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert queued_for(db_path, "refactorer")[0]["work_item"] == "cat-3-search"

    def test_an_unnamed_cycle_stores_null_not_the_placeholder(self, make_ctx, inbound, db_path):
        # `pending` is not a work item, it is the absence of one. Storing it is what put every
        # unrelated request in the live database into a single group.
        inbound(target="specifier", sender="human-in-the-loop", name="pending")

        role_scheduler.run_once(make_ctx(FakeWorker(), role="specifier"), SchedulerState())

        assert queued_for(db_path, "coder")[0]["work_item"] is None

    def test_the_placeholder_is_carried_in_the_header_when_nobody_names_it(
        self, make_ctx, inbound, db_path
    ):
        # The header keeps `pending` so the next role still knows it may name the work; only
        # the *column* goes NULL. They answer different questions.
        inbound(target="specifier", sender="human-in-the-loop", name="pending")

        role_scheduler.run_once(make_ctx(FakeWorker(), role="specifier"), SchedulerState())

        content = queued_for(db_path, "coder")[0]["content"]
        assert handoff.parse_handoff(content).handoff == "pending"

    def test_a_named_escalation_still_groups(self, make_ctx, inbound, db_path):
        inbound(name="cat-3-search")
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))

        role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert queued_for(db_path, "human-in-the-loop")[0]["work_item"] == "cat-3-search"

    def test_an_unnamed_escalation_stores_null(self, make_ctx, inbound, db_path):
        inbound(target="specifier", sender="human-in-the-loop", name="pending")
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))

        role_scheduler.run_once(make_ctx(fake, role="specifier"), SchedulerState())

        assert queued_for(db_path, "human-in-the-loop")[0]["work_item"] is None

    def test_the_cycle_guard_ignores_the_placeholder(self, make_ctx, inbound, db_path):
        # Otherwise every unrelated feature's intake shares one bucket, and maxCycles trips
        # on a swarm that is not looping at all.
        for _ in range(9):
            message_id = db.insert_handoff(
                db_path, "human-in-the-loop", "specifier", "old", "main", work_item="pending"
            )
            db.mark_processed(db_path, message_id)
        inbound(target="specifier", sender="human-in-the-loop", name="pending")

        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), role="specifier", max_cycles=2), SchedulerState()
        )

        assert result.outcome == role_scheduler.HANDED_OFF


class TestVerificationGate:
    """
    The quality gates were prose. In scheduler mode the only thing checked before a handoff
    was that the worker's last line said `done` -- so a worker that skipped every gate and
    claimed success was believed, in exactly the mode designed to run unattended.
    """

    def _verifier(self, *results):
        """A verify callable returning canned outcomes, recording how often it ran."""

        class Verifier:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                self.calls += 1
                index = min(self.calls - 1, len(results) - 1)
                return results[index]

        return Verifier()

    def _pass(self):
        return verify.VerifyResult(ok=True, output="")

    def _fail(self, output="2 tests failed"):
        return verify.VerifyResult(ok=False, output=output)

    def test_no_verify_command_behaves_exactly_as_before(self, make_ctx, inbound, db_path):
        inbound()
        result = role_scheduler.run_once(make_ctx(FakeWorker()), SchedulerState())
        assert result.outcome == role_scheduler.HANDED_OFF
        assert len(queued_for(db_path, "refactorer")) == 1

    def test_a_passing_verify_hands_off_normally(self, make_ctx, inbound, db_path):
        inbound()
        verifier = self._verifier(self._pass())

        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), run_verify=verifier), SchedulerState()
        )

        assert result.outcome == role_scheduler.HANDED_OFF
        assert verifier.calls == 1

    def test_a_failing_verify_costs_an_attempt_and_retries(self, make_ctx, inbound):
        inbound()
        fake = FakeWorker()
        verifier = self._verifier(self._fail(), self._pass())

        result = role_scheduler.run_once(make_ctx(fake, run_verify=verifier), SchedulerState())

        assert fake.calls == 2, "a failed gate must consume an attempt, like a blocked worker"
        assert result.outcome == role_scheduler.HANDED_OFF

    def test_the_worker_is_told_what_failed(self, make_ctx, inbound):
        inbound()
        fake = FakeWorker()
        verifier = self._verifier(
            self._fail("FAILED tests/test_orders.py::test_total"), self._pass()
        )

        role_scheduler.run_once(make_ctx(fake, run_verify=verifier), SchedulerState())

        assert "test_orders.py::test_total" in fake.prompts[1]
        assert "Previous attempt failed" in fake.prompts[1]

    def test_failing_twice_escalates_and_does_not_hand_off(self, make_ctx, inbound, db_path):
        inbound()
        verifier = self._verifier(self._fail())

        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), run_verify=verifier), SchedulerState()
        )

        assert result.outcome == role_scheduler.ESCALATED
        assert queued_for(db_path, "refactorer") == [], "unverified work must never be forwarded"
        assert len(queued_for(db_path, "human-in-the-loop")) == 1

    def test_the_escalation_carries_the_verify_output(self, make_ctx, inbound, db_path):
        inbound()
        verifier = self._verifier(self._fail("3 failed, 41 passed"))

        role_scheduler.run_once(make_ctx(FakeWorker(), run_verify=verifier), SchedulerState())

        assert "3 failed, 41 passed" in queued_for(db_path, "human-in-the-loop")[0]["content"]

    def test_it_does_not_run_when_the_worker_was_already_blocked(self, make_ctx, inbound):
        # Nothing to verify: the worker never claimed to have finished. Running the suite
        # anyway would burn the timeout to confirm what the worker already reported.
        inbound()
        verifier = self._verifier(self._pass())
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))

        role_scheduler.run_once(make_ctx(fake, run_verify=verifier), SchedulerState())

        assert verifier.calls == 0

    def test_cost_and_tokens_survive_a_failed_gate(self, make_ctx, inbound):
        # That work was really performed and really billed, whatever the gate concluded.
        inbound()
        usage = TokenUsage(input_tokens=100)
        fake = FakeWorker(
            worker(summary="done", cost_usd=1.5, tokens=usage),
            worker(summary="done", cost_usd=0.5, tokens=usage),
        )
        verifier = self._verifier(self._fail(), self._pass())

        result = role_scheduler.run_once(make_ctx(fake, run_verify=verifier), SchedulerState())

        assert result.cost_usd == pytest.approx(2.0)
        assert result.tokens.input_tokens == 200

    def test_a_timed_out_gate_is_a_failure_not_a_crash(self, make_ctx, inbound):
        inbound()
        timeout = verify.VerifyResult(ok=False, output="", timed_out=True)
        verifier = self._verifier(timeout)

        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), run_verify=verifier), SchedulerState()
        )

        assert result.outcome == role_scheduler.ESCALATED

    def test_the_pane_reports_that_it_is_verifying(self, make_ctx, inbound):
        inbound()
        seen = []
        ctx = make_ctx(
            FakeWorker(),
            run_verify=self._verifier(self._pass()),
            set_status=lambda state, **_kw: seen.append(state),
        )

        role_scheduler.run_once(ctx, SchedulerState())

        assert "verifying" in seen


class TestEscalationResume:
    """
    Escalation used to be a dead end: the inbound was marked `processed`, so there was nothing
    left to address, and the human's only move was `kiln send` -- a *new* work item carrying
    none of the failed cycle's context.
    """

    def _escalated(self, make_ctx, inbound, state=None):
        message_id, _ = inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no fixtures"))
        result = role_scheduler.run_once(make_ctx(fake), state or SchedulerState())
        return message_id, result

    def test_an_escalated_message_is_failed_not_processed(self, make_ctx, inbound, read_message):
        message_id, _ = self._escalated(make_ctx, inbound)

        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_FAILED
        assert "no fixtures" in stored["error"], "the reason must outlive the pane"

    def test_a_failed_message_is_not_re_served_on_its_own(self, make_ctx, inbound, db_path):
        self._escalated(make_ctx, inbound)
        assert db.fetch_and_deliver(db_path, "coder", "main") is None

    def test_a_resumed_message_is_worked_with_the_humans_guidance(self, make_ctx, inbound, db_path):
        from kiln.scheduler.infrastructure.cli import retry

        message_id, _ = self._escalated(make_ctx, inbound)
        retry.resume(db_path=db_path, message_id=message_id, guidance="fixtures live in tests/")

        fake = FakeWorker()
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())

        assert result.outcome == role_scheduler.HANDED_OFF
        assert "fixtures live in tests/" in fake.prompts[0], (
            "guidance must reach the worker, or resuming is just a re-run of what failed"
        )


class TestHaltedRoleParks:
    """
    The circuit breaker used to `return 1`, killing the pane -- and a `kiln retry` that
    re-queues a message for a dead scheduler puts it into a queue nobody reads. A halted role
    now stays alive and polls, accepting nothing except an explicitly resumed message.
    """

    def _halt(self, make_ctx, inbound):
        state = SchedulerState()
        for _ in range(3):
            inbound()
            fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
            role_scheduler.run_once(make_ctx(fake), state)
        return state

    def test_three_escalations_halt_the_role(self, make_ctx, inbound):
        assert self._halt(make_ctx, inbound).halted is True

    def test_a_halted_role_ignores_ordinary_work(self, make_ctx, inbound):
        state = self._halt(make_ctx, inbound)
        inbound()
        fake = FakeWorker()

        result = role_scheduler.run_once(make_ctx(fake), state)

        assert result.outcome == role_scheduler.HALTED
        assert fake.calls == 0, "it has failed three times; a fourth attempt helps nobody"

    def test_a_halted_role_does_not_consume_the_message_it_ignores(
        self, make_ctx, inbound, read_message
    ):
        # It must still be there for whoever resumes the role.
        state = self._halt(make_ctx, inbound)
        message_id, _ = inbound()

        role_scheduler.run_once(make_ctx(FakeWorker()), state)

        assert read_message(message_id)["status"] == db.STATUS_QUEUED

    def test_a_resume_wakes_it_up(self, make_ctx, inbound, db_path):
        from kiln.scheduler.infrastructure.cli import retry

        state = self._halt(make_ctx, inbound)
        failed_id = db.failed_messages(db_path, "main")[0]["id"]
        retry.resume(db_path=db_path, message_id=failed_id, guidance="try the other fixture")

        result = role_scheduler.run_once(make_ctx(FakeWorker()), state)

        assert result.outcome == role_scheduler.HANDED_OFF
        assert state.halted is False

    def test_resuming_re_arms_the_circuit_breaker(self, make_ctx, inbound, db_path):
        # Otherwise the next single escalation would halt the role again immediately.
        from kiln.scheduler.infrastructure.cli import retry

        state = self._halt(make_ctx, inbound)
        failed_id = db.failed_messages(db_path, "main")[0]["id"]
        retry.resume(db_path=db_path, message_id=failed_id, guidance="x")

        role_scheduler.run_once(make_ctx(FakeWorker()), state)

        assert state.consecutive_escalations == 0


class TestStatusReporting:
    def test_reports_lifecycle_states(self, make_ctx, inbound):
        inbound()
        seen = []
        # **kwargs, because delegation now also reports which attempt is running.
        ctx = make_ctx(FakeWorker(), set_status=lambda state, **_kw: seen.append(state))
        role_scheduler.run_once(ctx, SchedulerState())
        assert "receiving" in seen and "working" in seen and "handing-off" in seen

    def test_reports_blocked_on_escalation(self, make_ctx, inbound):
        inbound()
        seen = []
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
        ctx = make_ctx(fake, set_status=lambda state, **_kw: seen.append(state))
        role_scheduler.run_once(ctx, SchedulerState())
        assert "blocked" in seen

    def test_status_writer_failure_is_not_fatal(self, tmp_path):
        writer = role_scheduler.make_status_writer("coder", tmp_path / "absent.py")
        writer("working")  # must not raise

    def test_status_writer_invokes_the_script(self, tmp_path, monkeypatch):
        script = tmp_path / "set-status.py"
        script.write_text("import sys\n", encoding="utf-8")
        calls = {}
        monkeypatch.setattr(
            role_scheduler.subprocess,
            "run",
            lambda cmd, **kw: calls.setdefault("cmd", cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        role_scheduler.make_status_writer("coder", script)("working")
        assert calls["cmd"][-2:] == ["coder", "working"]

    def test_the_resolved_model_travels_with_every_status(self, tmp_path, monkeypatch):
        # Constant for the process, written on every status, exactly like the worker
        # timeout: nothing downstream can re-derive it, because `resolve_model` falls back
        # to the worker definition's frontmatter, which no reader parses.
        script = tmp_path / "set-status.py"
        script.write_text("import sys\n", encoding="utf-8")
        calls = {}
        monkeypatch.setattr(
            role_scheduler.subprocess,
            "run",
            lambda cmd, **kw: calls.setdefault("cmd", cmd) or subprocess.CompletedProcess(cmd, 0),
        )

        role_scheduler.make_status_writer("coder", script, model="claude-sonnet-5")("working")

        assert "--model=claude-sonnet-5" in calls["cmd"]

    def test_an_unset_model_passes_no_flag_at_all(self, tmp_path, monkeypatch):
        # An empty model is a real state for copilot/codex/grok -- "let the CLI choose" --
        # and `--model=` would write a blank into the status file rather than leaving it out.
        script = tmp_path / "set-status.py"
        script.write_text("import sys\n", encoding="utf-8")
        calls = {}
        monkeypatch.setattr(
            role_scheduler.subprocess,
            "run",
            lambda cmd, **kw: calls.setdefault("cmd", cmd) or subprocess.CompletedProcess(cmd, 0),
        )

        role_scheduler.make_status_writer("coder", script)("working")

        assert not any(part.startswith("--model=") for part in calls["cmd"])


class TestCommitPrefix:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("coder", "[Coder]"),
            ("specifier", "[Specifier]"),
            ("refactorer", "[Refactorer]"),
            ("architect", "[Architect]"),
            ("human-in-the-loop", "[Human-in-the-loop]"),
        ],
    )
    def test_matches_the_convention(self, role, expected):
        assert role_scheduler.commit_prefix(role) == expected


class TestMergeCommitMessage:
    def _inbound(self, **overrides):
        fields = dict(
            sender="specifier",
            handoff="order-intake",
            branch="main-specifier",
            commit="a1b2c3d4",
            is_ping=False,
            trail=(),
            raw="",
        )
        fields.update(overrides)
        return handoff.InboundHandoff(**fields)

    def test_subject_names_role_handoff_and_sender(self):
        message = role_scheduler.merge_commit_message("coder", self._inbound())
        subject = message.splitlines()[0]
        assert subject == "[Coder] Merge order-intake from specifier"

    def test_body_carries_the_detail_git_log_shows_on_demand(self):
        message = role_scheduler.merge_commit_message("coder", self._inbound())
        assert "Sender: specifier" in message
        assert "Handoff: order-intake" in message
        assert "Branch: main-specifier" in message
        assert "Commit: a1b2c3d4" in message

    def test_missing_fields_degrade_gracefully_instead_of_blank(self):
        message = role_scheduler.merge_commit_message(
            "coder", self._inbound(sender="", handoff="", branch="")
        )
        assert message.splitlines()[0] == "[Coder] Merge handoff from unknown"


class TestCycleResult:
    def test_reports_cost_and_attempts(self, make_ctx, inbound):
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", cost_usd=0.02), worker(summary="ok", cost_usd=0.03)
        )
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert result.attempts == 2
        assert result.cost_usd == pytest.approx(0.05)

    def test_tokens_are_summed_across_retries(self, make_ctx, inbound):
        # A retried cycle costs the sum of its attempts. Reporting only the successful one
        # would make the expensive cycles look like the cheap ones -- the opposite of what
        # token accounting exists for.
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", tokens=TokenUsage(input_tokens=100, output_tokens=20)),
            worker(summary="ok", tokens=TokenUsage(input_tokens=200, output_tokens=30)),
        )
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert result.tokens.total == 350

    def test_the_breakdown_survives_the_sum(self, make_ctx, inbound):
        # Summed field-wise, not collapsed: which kind of token a role burns is the
        # actionable part, and a total alone cannot distinguish cache reads from bloat.
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", tokens=TokenUsage(input_tokens=10, cache_read_tokens=90)),
            worker(summary="ok", tokens=TokenUsage(input_tokens=5, cache_read_tokens=400)),
        )
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert result.tokens == TokenUsage(input_tokens=15, cache_read_tokens=490)

    def test_an_attempt_reporting_no_usage_contributes_nothing(self, make_ctx, inbound):
        # `tokens=None` means the backend said nothing, which must not be counted as a
        # zero-token attempt nor crash the sum.
        inbound()
        fake = FakeWorker(
            worker(STATUS_BLOCKED, "no", tokens=None),
            worker(summary="ok", tokens=TokenUsage(input_tokens=42)),
        )
        result = role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert result.tokens == TokenUsage(input_tokens=42)

    def test_tokens_default_to_empty_when_no_backend_reports_them(self, make_ctx, inbound):
        inbound()
        result = role_scheduler.run_once(make_ctx(FakeWorker(worker())), SchedulerState())
        assert result.tokens == TokenUsage()
        assert result.tokens.total == 0

    def test_result_is_immutable(self):
        with pytest.raises(AttributeError):
            CycleResult(role_scheduler.IDLE).outcome = "x"  # type: ignore[misc]
