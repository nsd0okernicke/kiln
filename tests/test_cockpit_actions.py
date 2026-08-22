"""
The cockpit's write half — every button, against a real queue.

Real SQLite, never a mock: these actions exist precisely to delegate to `scheduler.send`,
`scheduler.retry` and `launcher.stop`, so a test that mocked the delegate would assert the
one thing the module does not do.
"""

from __future__ import annotations

import pytest
from cockpit import actions
from scheduler import db

pytestmark = pytest.mark.integration


@pytest.fixture
def ctx(tmp_path, db_path):
    sessions = tmp_path / "sessions"
    sessions.write_text(
        "1\thuman-in-the-loop\tclaude\tHuman In The Loop\n2\tcoder\tclaude\tCoder\n",
        encoding="utf-8",
    )
    return actions.ActionContext(
        db_path=db_path,
        branch="main",
        human_role="human-in-the-loop",
        intake_role="specifier",
        sessions_file=sessions,
    )


class TestNewTask:
    def test_a_task_reaches_the_role_routing_names(self, ctx, db_path):
        result = actions.new_task(ctx, summary="Add order intake")

        message = db.get_message(db_path, result["message_id"])
        assert message["target"] == "specifier"
        assert message["sender"] == "human-in-the-loop"
        assert "Add order intake" in message["content"]

    def test_an_unnamed_task_carries_no_work_item(self, ctx, db_path):
        # `pending` is the placeholder the specifier replaces; storing it as a work item
        # would put every unrelated new request in one grouping bucket, which is what the
        # max-cycles and cost guards count against.
        result = actions.new_task(ctx, summary="Add order intake")

        assert db.get_message(db_path, result["message_id"])["work_item"] is None

    def test_a_named_task_keeps_the_name_it_was_given(self, ctx, db_path):
        result = actions.new_task(ctx, summary="Fix the totals", name="ORDER-INTAKE")

        assert db.get_message(db_path, result["message_id"])["work_item"] == "ORDER-INTAKE"

    def test_an_empty_task_is_refused_rather_than_queued(self, ctx, db_path):
        with pytest.raises(actions.ActionError):
            actions.new_task(ctx, summary="   ")

        assert db.recent_messages(db_path, "main") == []

    def test_a_cockpit_with_no_intake_role_says_so_instead_of_guessing(self, ctx):
        # Guessing `specifier` would queue work for a role this profile may not launch, and
        # nothing polls a queue nobody owns: the message would vanish with no error anywhere.
        with pytest.raises(actions.ActionError, match="intake role"):
            actions.new_task(
                actions.ActionContext(
                    db_path=ctx.db_path, branch="main", human_role="human-in-the-loop",
                    intake_role="", sessions_file=ctx.sessions_file,
                ),
                summary="Add order intake",
            )


class TestChat:
    def test_a_note_goes_to_the_human_role_not_the_intake_role(self, ctx, db_path):
        result = actions.chat(ctx, summary="how is the refactor going?")

        message = db.get_message(db_path, result["message_id"])
        assert message["target"] == "human-in-the-loop"

    def test_the_sender_identifies_the_browser(self, ctx, db_path):
        # A role must not appear to send itself mail, and the inbox pane prints the sender.
        result = actions.chat(ctx, summary="ping")

        assert db.get_message(db_path, result["message_id"])["sender"] == "cockpit"

    def test_an_empty_note_is_refused(self, ctx):
        with pytest.raises(actions.ActionError):
            actions.chat(ctx, summary="")


class TestRetryMessage:
    @pytest.fixture
    def failed_id(self, db_path, add_message):
        message_id = add_message(target="coder", work_item="ALPHA", content="original brief")
        db.mark_failed(db_path, message_id, "worker gave up")
        return message_id

    def test_it_re_queues_the_same_row(self, ctx, db_path, failed_id):
        # The same row, not a new one: a fresh insert would look like brand-new work to every
        # guard that counts per work item.
        actions.retry_message(ctx, message_id=failed_id, guidance="try the other approach")

        message = db.get_message(db_path, failed_id)
        assert message["status"] == db.STATUS_QUEUED
        assert message["work_item"] == "ALPHA"

    def test_guidance_reaches_the_worker(self, ctx, db_path, failed_id):
        actions.retry_message(ctx, message_id=failed_id, guidance="try the other approach")

        assert "try the other approach" in db.get_message(db_path, failed_id)["content"]

    def test_the_short_id_the_page_shows_is_accepted(self, ctx, failed_id):
        # The board and the Attention rail print eight characters, so the endpoint has to
        # take what it displays.
        result = actions.retry_message(ctx, message_id=failed_id[:8])

        assert result["message_id"] == failed_id

    def test_an_unknown_id_is_refused(self, ctx):
        with pytest.raises(actions.ActionError, match="no failed message"):
            actions.retry_message(ctx, message_id="deadbeef")

    def test_a_healthy_message_cannot_be_retried(self, ctx, db_path, add_message):
        # Re-queueing a message that is merely `processing` would hand a live scheduler a
        # second copy of the work it is already doing.
        message_id = add_message(target="coder", status=db.STATUS_PROCESSING)

        with pytest.raises(actions.ActionError):
            actions.retry_message(ctx, message_id=message_id)


class TestTeardown:
    def test_it_refuses_without_the_confirmation(self, ctx, monkeypatch):
        called = []
        monkeypatch.setattr(actions.stop, "stop_all", lambda *a, **k: called.append(a))

        with pytest.raises(actions.ActionError):
            actions.teardown(ctx, confirm="yes")

        assert called == []

    def test_it_stops_every_kiln_process_when_confirmed(self, ctx, monkeypatch):
        monkeypatch.setattr(actions.stop, "stop_all", lambda roles: [101, 102])

        assert actions.teardown(ctx, confirm="TEARDOWN")["stopped"] == 2

    def test_it_hands_the_projects_roles_to_the_tmux_cleanup(self, ctx, monkeypatch):
        # tmux sessions are named per role and are not found by command-line matching, so
        # the sessions file is the only thing that can close them.
        seen = {}
        monkeypatch.setattr(
            actions.stop, "stop_all", lambda roles: seen.setdefault("roles", roles) or []
        )

        actions.teardown(ctx, confirm="TEARDOWN")

        assert seen["roles"] == ["human-in-the-loop", "coder"]

    def test_check_confirmation_is_what_rejects_early(self):
        # The server calls this before replying and `teardown` only afterwards, because
        # `stop_all` kills the process that would otherwise write the reply.
        with pytest.raises(actions.ActionError):
            actions.check_confirmation("")

        assert actions.check_confirmation("TEARDOWN") is None
