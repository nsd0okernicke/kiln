"""
The human's inbox and the send CLI.

These replace the wrapper-mode `human-in-the-loop` role's messaging half. The bug they exist
to prevent is specific and was observed live: a coder escalation sat `queued` with
`delivered_at = NULL` for a day, because the only thing that could have received it was an
LLM session that has to choose between listening (blocked in `wait_for_message()`, which
never times out) and being available to its human.
"""

from __future__ import annotations

import pytest
from scheduler import db, handoff, inbox, send

pytestmark = pytest.mark.integration


def _message(sender="coder", summary="did the thing", escalation=False, ping=False):
    return handoff.format_handoff(
        sender=sender, handoff="pending", branch="main", commit="abc1234",
        summary=summary, next_role="human-in-the-loop", timestamp="2026-08-08 10:00:00",
        escalation=escalation, ping=ping,
    )


@pytest.fixture
def ctx(db_path):
    lines: list[str] = []
    context = inbox.InboxContext(
        role="human-in-the-loop", branch="main", db_path=db_path,
        bell=False, emit=lines.append,
    )
    context.lines = lines  # type: ignore[attr-defined]
    return context


def _queue(db_path, content, sender="coder"):
    return db.insert_handoff(db_path, sender, "human-in-the-loop", content, "main")


class TestDelivery:
    def test_an_empty_inbox_returns_nothing(self, ctx):
        assert inbox.poll_once(ctx) is None

    def test_a_queued_message_is_shown(self, ctx):
        _queue(ctx.db_path, _message(summary="finished CAT-3"))
        assert inbox.poll_once(ctx) is not None
        assert "finished CAT-3" in "\n".join(ctx.lines)

    def test_the_sender_is_named(self, ctx):
        # "who is asking me for something" is the first thing a human needs.
        _queue(ctx.db_path, _message(sender="architect"), sender="architect")
        inbox.poll_once(ctx)
        assert "architect" in "\n".join(ctx.lines)

    def test_a_shown_message_is_marked_processed(self, ctx):
        message_id = _queue(ctx.db_path, _message())
        inbox.poll_once(ctx)
        row = db.fetch_and_deliver(ctx.db_path, "human-in-the-loop", "main")
        assert row is None, "a processed message must not be served again"
        assert message_id

    def test_the_same_message_is_not_shown_twice(self, ctx):
        # Left `delivered`, fetch_and_deliver re-serves it every poll, forever.
        _queue(ctx.db_path, _message())
        inbox.poll_once(ctx)
        ctx.lines.clear()
        assert inbox.poll_once(ctx) is None
        assert ctx.lines == []

    def test_messages_for_other_roles_are_ignored(self, ctx):
        db.insert_handoff(ctx.db_path, "specifier", "coder", _message(), "main")
        assert inbox.poll_once(ctx) is None

    def test_messages_on_another_branch_are_ignored(self, ctx):
        # Messages are branch-scoped; an inbox on the wrong branch looks simply empty.
        db.insert_handoff(
            ctx.db_path, "coder", "human-in-the-loop", _message(), "feature-x"
        )
        assert inbox.poll_once(ctx) is None


class TestClassification:
    def test_an_escalation_is_called_out(self, ctx):
        # This is the case that matters: the swarm has stopped and is asking for help.
        _queue(ctx.db_path, _message(escalation=True, summary="merge failed"))
        inbox.poll_once(ctx)
        shown = "\n".join(ctx.lines)
        assert "ESCALATION" in shown
        assert inbox.ICON_ESCALATION in shown

    def test_a_routine_handoff_is_not_dressed_as_an_emergency(self, ctx):
        _queue(ctx.db_path, _message())
        inbox.poll_once(ctx)
        assert "ESCALATION" not in "\n".join(ctx.lines)

    def test_a_ping_is_distinguishable(self, ctx):
        _queue(ctx.db_path, _message(ping=True))
        inbox.poll_once(ctx)
        assert inbox.ICON_PING in "\n".join(ctx.lines)

    def test_the_body_is_preserved_verbatim(self, ctx):
        # Reformatting risks dropping something the human needed.
        content = _message(summary="exact wording matters")
        _queue(ctx.db_path, content)
        inbox.poll_once(ctx)
        assert content.rstrip() in "\n".join(ctx.lines)


class TestReceiveDoesTheWorkNotJustTheNotice:
    """
    `human-in-the-loop` is a real role in the graph, not a notification target.

    It works in the project root on the base branch, so an inbound handoff has to be merged
    into *its* tree — `/kiln-receive` step 4 — or the work a person is being asked to review
    is not actually present. Showing the message and marking it processed, which is all the
    first version did, left the human with a description of work they could not see.
    """

    @pytest.fixture
    def human(self, db_path, git_repo):
        lines: list[str] = []
        ctx = inbox.InboxContext(
            role="human-in-the-loop", branch="main", db_path=db_path,
            worktree=git_repo, bell=False, emit=lines.append,
        )
        ctx.lines = lines  # type: ignore[attr-defined]
        return ctx

    def _sender_commit(self, git_repo, git_cmd, filename="feature.py"):
        git_cmd(git_repo, "checkout", "-q", "-b", "main-architect")
        (git_repo / filename).write_text("built by the swarm\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "[Architect] cycle complete")
        from scheduler import git_ops

        commit = git_ops.head_commit(git_repo)
        git_cmd(git_repo, "checkout", "-q", "main")
        (git_repo / filename).unlink(missing_ok=True)
        return commit

    def test_the_inbound_commit_is_merged_into_the_human_tree(
        self, human, git_repo, git_cmd
    ):
        commit = self._sender_commit(git_repo, git_cmd)
        content = handoff.format_handoff(
            sender="architect", handoff="CAT-3", branch="main-architect", commit=commit,
            summary="cycle complete", next_role="human-in-the-loop",
            timestamp="2026-08-09 10:00:00",
        )
        _queue(human.db_path, content, sender="architect")

        inbox.poll_once(human)

        assert (git_repo / "feature.py").is_file(), "the work must land in the human's tree"

    def test_the_merge_is_reported(self, human, git_repo, git_cmd):
        commit = self._sender_commit(git_repo, git_cmd)
        _queue(human.db_path, handoff.format_handoff(
            sender="architect", handoff="CAT-3", branch="main-architect", commit=commit,
            summary="done", next_role="human-in-the-loop", timestamp="t",
        ), sender="architect")

        inbox.poll_once(human)
        assert "merged" in "\n".join(human.lines)

    def test_the_merge_commit_names_the_role_handoff_and_sender(self, human, git_repo, git_cmd):
        # Otherwise it's git's generic "Merge commit '<hash>' into <branch>" -- identical for
        # every merge and useless in `git log` without cross-referencing messages.db by hand.
        from scheduler import git_ops

        commit = self._sender_commit(git_repo, git_cmd)
        _queue(human.db_path, handoff.format_handoff(
            sender="architect", handoff="CAT-3", branch="main-architect", commit=commit,
            summary="done", next_role="human-in-the-loop", timestamp="t",
        ), sender="architect")

        inbox.poll_once(human)

        # HEAD is `record_provenance`'s history link; the content commit sits beneath it.
        subject = git_ops.run_git(["log", "-1", "--format=%s", "HEAD^1"], git_repo).stdout
        assert subject == "[Human-in-the-loop] Merge CAT-3 from architect"

    def test_the_message_is_persisted_for_the_humans_own_session(self, human, git_repo):
        # tmp/handoff-in.md is how a person's Claude session picks up what arrived — there
        # is no way to inject a message into a running session.
        _queue(human.db_path, _message(summary="read me"))
        inbox.poll_once(human)
        saved = git_repo / "tmp" / "handoff-in.md"
        assert saved.is_file()
        assert "read me" in saved.read_text(encoding="utf-8")

    def test_a_failed_merge_is_shouted_about(self, human, git_repo):
        # An unmergeable commit must not look like a normal delivery.
        _queue(human.db_path, handoff.format_handoff(
            sender="architect", handoff="x", branch="main-architect", commit="0" * 40,
            summary="unmergeable", next_role="human-in-the-loop", timestamp="t",
        ), sender="architect")

        inbox.poll_once(human)
        shown = "\n".join(human.lines)
        assert "MERGE FAILED" in shown
        assert "NOT in your tree" in shown

    def test_a_failed_merge_still_leaves_the_queue_clean(self, human):
        # Left queued it would be re-served every poll forever, by nobody.
        _queue(human.db_path, handoff.format_handoff(
            sender="architect", handoff="x", branch="b", commit="0" * 40,
            summary="unmergeable", next_role="human-in-the-loop", timestamp="t",
        ), sender="architect")

        inbox.poll_once(human)
        assert db.count_queued(human.db_path, "human-in-the-loop", "main") == 0

    def test_a_message_with_no_commit_needs_no_merge(self, human):
        # A human's own opening request, or a ping, carries nothing to merge.
        _queue(human.db_path, handoff.format_handoff(
            sender="specifier", handoff="x", branch="main", commit="",
            summary="just telling you", next_role="human-in-the-loop", timestamp="t",
        ), sender="specifier")

        assert inbox.poll_once(human) is not None
        assert "MERGE FAILED" not in "\n".join(human.lines)

    def test_merging_can_be_turned_off(self, human, git_repo, git_cmd):
        commit = self._sender_commit(git_repo, git_cmd)
        human.merge = False
        _queue(human.db_path, handoff.format_handoff(
            sender="architect", handoff="x", branch="b", commit=commit,
            summary="done", next_role="human-in-the-loop", timestamp="t",
        ), sender="architect")

        inbox.poll_once(human)
        assert not (git_repo / "feature.py").exists()

    def test_without_a_worktree_it_only_displays(self, ctx):
        # `kiln inbox` run ad hoc from anywhere must not touch a repo it was not given.
        _queue(ctx.db_path, _message())
        assert inbox.poll_once(ctx) is not None
        assert "merged" not in "\n".join(ctx.lines)


class TestBell:
    def test_rings_the_terminal_by_default(self, db_path):
        lines: list[str] = []
        context = inbox.InboxContext(
            role="human-in-the-loop", branch="main", db_path=db_path, emit=lines.append
        )
        _queue(db_path, _message())
        inbox.poll_once(context)
        assert inbox.BELL in lines, "nobody is watching this pane; that is the point of it"

    def test_can_be_silenced(self, ctx):
        _queue(ctx.db_path, _message())
        inbox.poll_once(ctx)
        assert inbox.BELL not in ctx.lines


class TestInboxCli:
    def test_once_mode_drains_and_exits(self, db_path, capsys):
        _queue(db_path, _message(summary="drain me"))
        code = inbox.main([
            "--db-path", str(db_path), "--branch", "main",
            "--once", "--no-status-bar", "--no-bell",
        ])
        assert code == 0
        assert "drain me" in capsys.readouterr().out

    def test_defaults_to_the_human_role(self):
        assert inbox.build_parser().parse_args(["--db-path", "x"]).role == "human-in-the-loop"


class TestSend:
    def test_queues_a_message_the_target_can_fetch(self, db_path):
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="specifier",
            summary="build the search feature", branch="main",
        )
        fetched = db.fetch_and_deliver(db_path, "specifier", "main")
        assert fetched is not None
        assert "build the search feature" in fetched["content"]

    def test_the_message_parses_as_a_handoff(self, db_path):
        # The receiving scheduler parses it with the same code as an agent-sent handoff, so
        # a human-authored message must be structurally identical.
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="specifier",
            summary="do a thing", branch="main",
        )
        fetched = db.fetch_and_deliver(db_path, "specifier", "main")
        parsed = handoff.parse_handoff(fetched["content"])
        assert parsed.sender == "human-in-the-loop"
        assert parsed.handoff == send.PENDING_HANDOFF

    def test_a_new_request_has_no_work_item_yet(self, db_path):
        # The specifier is what invents the name, so the intake hop legitimately has none.
        # This is the one deliberate NULL; everything after it must carry a value.
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="specifier",
            summary="new idea", branch="main",
        )
        assert db.cycles_by_work_item(db_path, "main") == {}

    def test_a_named_handoff_is_stored_as_the_work_item(self, db_path):
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="coder",
            summary="carry on", branch="main", handoff_name="CAT-3 search",
        )
        assert db.cycles_by_work_item(db_path, "main") == {"CAT-3 search": 1}

    def test_a_new_request_carries_no_commit_and_is_not_mergeable(self, db_path):
        # A human's opening request has nothing to merge; the receiver must not try.
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="specifier",
            summary="new idea", branch="main",
        )
        fetched = db.fetch_and_deliver(db_path, "specifier", "main")
        assert handoff.parse_handoff(fetched["content"]).is_mergeable is False

    def test_a_commit_can_be_attached(self, db_path):
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="coder",
            summary="merge this", branch="main", commit="deadbeef",
        )
        fetched = db.fetch_and_deliver(db_path, "coder", "main")
        assert handoff.parse_handoff(fetched["content"]).is_mergeable is True

    def test_escalation_flag_round_trips(self, db_path):
        send.send(
            db_path=db_path, sender="human-in-the-loop", target="coder",
            summary="urgent", branch="main", escalation=True,
        )
        fetched = db.fetch_and_deliver(db_path, "coder", "main")
        assert handoff.is_escalation(fetched["content"]) is True

    def test_build_message_is_pure(self):
        # No DB needed to check the wire format.
        rendered = send.build_message(
            sender="human-in-the-loop", target="specifier",
            summary="hello", branch="main", timestamp="2026-01-01 00:00:00",
        )
        assert "Sender: human-in-the-loop" in rendered
        assert "hello" in rendered


class TestSendCli:
    def test_reports_the_queued_message(self, db_path, capsys):
        code = send.main([
            "Build the thing", "--to", "specifier", "--db-path", str(db_path),
        ])
        assert code == 0
        assert "specifier" in capsys.readouterr().out

    def test_a_missing_queue_is_explained_not_crashed(self, tmp_path, capsys):
        # sqlite would happily create an empty file and the message would vanish.
        code = send.main([
            "hi", "--to", "specifier", "--db-path", str(tmp_path / "nope.db"),
        ])
        assert code == 1
        assert "Launch the swarm first" in capsys.readouterr().err

    def test_target_is_required(self):
        with pytest.raises(SystemExit):
            send.build_parser().parse_args(["summary only"])


class TestTheBugThisReplaces:
    def test_an_escalation_reaches_a_human_without_any_llm_session(self, db_path, capsys):
        """
        The regression, end to end.

        Observed live: `coder -> human-in-the-loop` sat `queued`, `delivered_at = NULL`, for
        a day. No LLM, no MCP server and no agent CLI take part in this test — which is the
        entire point.
        """
        db.insert_handoff(
            db_path, "coder", "human-in-the-loop",
            _message(escalation=True, summary="merge of abc failed"), "main",
        )

        assert inbox.main([
            "--db-path", str(db_path), "--once", "--no-status-bar", "--no-bell",
        ]) == 0

        shown = capsys.readouterr().out
        assert "merge of abc failed" in shown
        assert db.count_queued(db_path, "human-in-the-loop", "main") == 0
