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
from scheduler import db, git_ops, handoff, role_scheduler
from scheduler.adapters import TokenUsage
from scheduler.adapters.claude_adapter import WorkerInvocation
from scheduler.role_scheduler import CycleResult, SchedulerContext, SchedulerState
from scheduler.routing import parse_routing_table
from scheduler.status_contract import STATUS_BLOCKED, STATUS_DONE, WorkerResult
from scheduler.worker_prompt import WorkerDefinition

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


def worker(status=STATUS_DONE, summary="did the work", **kwargs):
    """Build a canned worker outcome."""
    kwargs.setdefault("raw_output", f"KILN-STATUS: {status} {summary}")
    return WorkerInvocation(
        result=WorkerResult(status=status, summary=summary, sentinel_found=True), **kwargs
    )


class FakeWorker:
    """Returns queued outcomes in order, recording the prompts it was given."""

    def __init__(self, *outcomes, edits_file=None):
        self.outcomes = list(outcomes)
        self.prompts = []
        self.edits_file = edits_file

    def __call__(self, *, prompt, attempt=1):
        self.prompts.append(prompt)
        if self.edits_file:
            self.edits_file.write_text("worker output\n", encoding="utf-8")
        return self.outcomes.pop(0) if self.outcomes else worker()

    @property
    def calls(self):
        return len(self.prompts)


@pytest.fixture
def make_ctx(db_path, git_repo):
    def _make(run_worker, role="coder", **overrides):
        args = {
            "role": role,
            "branch": "main",
            "db_path": db_path,
            "worktree": git_repo,
            "routing": ROUTING,
            "definition": DEFINITION,
            "run_worker": run_worker,
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

    def test_worker_that_changes_nothing_still_hands_off(self, make_ctx, inbound, db_path):
        # Legitimate outcome: an architect can validate and find nothing to change.
        inbound()
        result = role_scheduler.run_once(make_ctx(FakeWorker(worker())), SchedulerState())
        assert result.outcome == role_scheduler.HANDED_OFF
        assert handoff.parse_handoff(queued_for(db_path, "refactorer")[0]["content"]).commit

    def test_commits_work_the_worker_left_uncommitted(self, make_ctx, inbound, git_repo):
        inbound()
        fake = FakeWorker(worker(), edits_file=git_repo / "feature.py")
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert git_ops.has_pending_changes(git_repo) is False
        assert (git_repo / "feature.py").read_text(encoding="utf-8") == "worker output\n"

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
        assert any(
            s.startswith("[Coder] Merge order-intake from specifier") for s in subjects
        )
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
        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), role="specifier"), SchedulerState()
        )
        assert result.target == "human-in-the-loop"
        assert len(queued_for(db_path, "human-in-the-loop")) == 1

    def test_specifier_sends_normal_requests_to_the_coder(self, make_ctx, inbound):
        inbound(target="specifier", sender="human-in-the-loop")
        result = role_scheduler.run_once(
            make_ctx(FakeWorker(), role="specifier"), SchedulerState()
        )
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

    def test_inbound_message_never_wedges_in_processing(self, make_ctx, inbound, read_message):
        message_id, _ = inbound()
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
        role_scheduler.run_once(make_ctx(fake), SchedulerState())
        assert read_message(message_id)["status"] == db.STATUS_PROCESSED


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


class TestStatusReporting:
    def test_reports_lifecycle_states(self, make_ctx, inbound):
        inbound()
        seen = []
        ctx = make_ctx(FakeWorker(), set_status=seen.append)
        role_scheduler.run_once(ctx, SchedulerState())
        assert "receiving" in seen and "working" in seen and "handing-off" in seen

    def test_reports_blocked_on_escalation(self, make_ctx, inbound):
        inbound()
        seen = []
        fake = FakeWorker(worker(STATUS_BLOCKED, "no"), worker(STATUS_BLOCKED, "no"))
        role_scheduler.run_once(make_ctx(fake, set_status=seen.append), SchedulerState())
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
            lambda cmd, **kw: calls.setdefault("cmd", cmd)
            or subprocess.CompletedProcess(cmd, 0),
        )
        role_scheduler.make_status_writer("coder", script)("working")
        assert calls["cmd"][-2:] == ["coder", "working"]


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
            sender="specifier", handoff="order-intake", branch="main-specifier",
            commit="a1b2c3d4", is_ping=False, trail=(), raw="",
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
