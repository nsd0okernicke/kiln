"""
The cockpit's write half — every button, against a real queue.

Real SQLite, never a mock: these actions exist precisely to delegate to `scheduler.send`,
`scheduler.retry` and launcher stop, so a test that mocked the delegate would assert the
one thing the module does not do.
"""

from __future__ import annotations

import pytest

from kiln.cockpit.application import actions
from kiln.cockpit.infrastructure import actions_gateway
from kiln.cockpit.infrastructure.actions_gateway import KilnActionGateway
from kiln.scheduler.infrastructure.persistence import db

pytestmark = pytest.mark.integration


@pytest.fixture
def ctx(tmp_path, db_path):
    sessions = tmp_path / "sessions"
    # A launched swarm's inventory, including the passive panes: `send_to` checks its target
    # against this, and teardown needs every row.
    sessions.write_text(
        "1\thuman-in-the-loop\tclaude\tHuman In The Loop\tagent\n"
        "2\tspecifier\tclaude\tSpecifier\tpython\n"
        "3\tcoder\tclaude\tCoder\tpython\n"
        "4\tcockpit\tclaude\tCockpit\tcockpit\n",
        encoding="utf-8",
    )
    return actions.ActionContext(
        db_path=db_path,
        branch="main",
        human_role="human-in-the-loop",
        intake_role="specifier",
        sessions_file=sessions,
        gateway=KilnActionGateway(),
    )


class TestSendTo:
    """
    Addressing a role directly — "specifier, restart with CAT-3". The general form of
    `chat` and operator interventions, and the same insert `kiln send --to <role>` makes.
    """

    def test_it_queues_for_the_role_the_operator_chose(self, ctx, db_path):
        result = actions.send_to(ctx, target="coder", summary="restart with CAT-3")

        message = db.get_message(db_path, result["message_id"])
        assert message["target"] == "coder"
        assert "restart with CAT-3" in message["content"]

    def test_an_existing_work_item_is_carried_through_unchanged(self, ctx, db_path):
        # The whole point of naming it: `resolve_work_item` returns a non-`pending` inbound
        # name untouched, so the card keeps one identity instead of starting a second.
        result = actions.send_to(ctx, target="specifier", summary="restart", work_item="CAT-3")

        assert db.get_message(db_path, result["message_id"])["work_item"] == "CAT-3"

    def test_the_human_is_the_sender_when_directing_a_worker(self, ctx):
        assert actions.send_to(ctx, target="coder", summary="go")["sender"] == ("human-in-the-loop")

    def test_the_cockpit_is_the_sender_when_writing_to_the_humans_own_queue(self, ctx):
        # A role must not appear to mail itself, and the inbox pane prints the sender.
        result = actions.send_to(ctx, target="human-in-the-loop", summary="note to self")

        assert result["sender"] == "cockpit"

    def test_a_passive_pane_is_refused(self, ctx, db_path):
        # It runs no agent, so nothing would ever read the message: the insert would succeed,
        # report success, and the work would stop dead with no error anywhere.
        with pytest.raises(actions.ActionError, match="runs no agent"):
            actions.send_to(ctx, target="cockpit", summary="hello")

        assert db.recent_messages(db_path, "main") == []

    def test_an_unknown_role_is_refused_and_names_the_real_ones(self, ctx):
        with pytest.raises(actions.ActionError, match="not a role in this swarm") as caught:
            actions.send_to(ctx, target="speciifer", summary="typo")

        assert "specifier" in str(caught.value)

    def test_an_empty_target_is_refused(self, ctx):
        with pytest.raises(actions.ActionError, match="no target role"):
            actions.send_to(ctx, target="  ", summary="hello")

    def test_an_empty_message_is_refused(self, ctx, db_path):
        with pytest.raises(actions.ActionError):
            actions.send_to(ctx, target="coder", summary="   ")

        assert db.recent_messages(db_path, "main") == []

    def test_a_work_item_name_that_would_poison_the_grouping_key_is_refused(self, ctx):
        # A sentence in the `work_item` column becomes the key cost, laps and the board card
        # are grouped by.
        with pytest.raises(actions.ActionError, match="usable work-item name"):
            actions.send_to(
                ctx,
                target="coder",
                summary="go",
                work_item="please restart this with the CAT-3 spec, thanks!",
            )

    def test_the_placeholder_is_accepted_and_stored_as_no_work_item(self, ctx, db_path):
        # "let the specifier name it" is a legitimate answer from a human, unlike from a
        # worker that was asked to invent one.
        result = actions.send_to(
            ctx, target="specifier", summary="something new", work_item="pending"
        )

        assert db.get_message(db_path, result["message_id"])["work_item"] is None


class TestBacklogTask:
    def test_creation_does_not_queue_a_message(self, ctx, db_path):
        result = actions.create_task(
            ctx, work_item="ORDER-INTAKE", title="Order intake", body="Add the workflow"
        )

        assert result["status"] == "backlog"
        assert db.recent_messages(db_path, "main") == []

    def test_a_task_can_be_refined_before_handoff(self, ctx):
        task = actions.create_task(ctx, work_item="CAT-2", title="Old", body="First draft")

        updated = actions.update_task(
            ctx, identifier=task["id"], title="Search by author", body="Final story"
        )

        assert updated["work_item"] == "CAT-2"
        assert (updated["title"], updated["body"]) == ("Search by author", "Final story")

    def test_handoff_queues_the_snapshot_once(self, ctx, db_path):
        task = actions.create_task(ctx, work_item="CAT-2", title="Search", body="By author")

        result = actions.handoff_task(ctx, identifier=task["id"])
        message = db.get_message(db_path, result["message_id"])

        assert result["status"] == "active"
        assert message["target"] == "specifier"
        assert message["work_item"] == "CAT-2"
        assert "Search\n\nBy author" in message["content"]
        with pytest.raises(actions.ActionError, match="already left"):
            actions.handoff_task(ctx, identifier=task["id"])
        assert len(db.recent_messages(db_path, "main")) == 1

    def test_archive_is_non_destructive(self, ctx):
        task = actions.create_task(ctx, work_item="CAT-9", title="Maybe", body="Later")

        archived = actions.archive_task(ctx, identifier=task["id"])

        assert archived["status"] == "archived"

    def test_invalid_and_duplicate_names_are_refused(self, ctx):
        with pytest.raises(actions.ActionError, match="work-item name"):
            actions.create_task(ctx, work_item="!", title="Bad", body="Bad")
        actions.create_task(ctx, work_item="CAT-2", title="One", body="One")
        with pytest.raises(actions.ActionError, match="already exists"):
            actions.create_task(ctx, work_item="CAT-2", title="Two", body="Two")


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
        monkeypatch.setattr(actions_gateway.stop, "stop_all", lambda *a, **k: called.append(a))

        with pytest.raises(actions.ActionError):
            actions.teardown(ctx, confirm="yes")

        assert called == []

    def test_it_stops_every_kiln_process_when_confirmed(self, ctx, monkeypatch):
        monkeypatch.setattr(actions_gateway.stop, "stop_all", lambda roles: [101, 102])

        assert actions.teardown(ctx, confirm="TEARDOWN")["stopped"] == 2

    def test_it_hands_the_projects_roles_to_the_tmux_cleanup(self, ctx, monkeypatch):
        # tmux sessions are named per role and are not found by command-line matching, so
        # the sessions file is the only thing that can close them.
        seen = {}
        monkeypatch.setattr(
            actions_gateway.stop, "stop_all", lambda roles: seen.setdefault("roles", roles) or []
        )

        actions.teardown(ctx, confirm="TEARDOWN")

        # Every row, passive panes included: a cockpit or inbox tmux session left running is
        # one nothing else will ever close.
        assert seen["roles"] == ["human-in-the-loop", "specifier", "coder", "cockpit"]

    def test_check_confirmation_is_what_rejects_early(self):
        # The server calls this before replying and `teardown` only afterwards, because
        # `stop_all` kills the process that would otherwise write the reply.
        with pytest.raises(actions.ActionError):
            actions.check_confirmation("")

        assert actions.check_confirmation("TEARDOWN") is None
