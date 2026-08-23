"""
`kiln retry` — resuming a role that escalated, rather than starting a new work item.

The behaviour that matters is identity: the *same* row goes back, so everything keyed on the
work item (lap counts, spend, history) still refers to one piece of work.
"""

from __future__ import annotations

import pytest

from kiln.scheduler.domain import handoff
from kiln.scheduler.infrastructure.cli import retry
from kiln.scheduler.infrastructure.persistence import db


@pytest.fixture
def failed(db_path):
    """One escalated message, as `_escalate` would leave it."""

    def _fail(*, target="coder", work_item="add-login", error="worker blocked: no fixtures"):
        content = handoff.format_handoff(
            sender="specifier",
            handoff=work_item,
            branch="main",
            commit="abc1234",
            summary="Please implement it.",
            next_role=target,
            timestamp="2026-08-07 10:00:00",
        )
        message_id = db.insert_handoff(
            db_path, "specifier", target, content, "main", work_item=work_item
        )
        db.mark_failed(db_path, message_id, error)
        return message_id

    return _fail


class TestResume:
    def test_it_re_queues_the_failed_message(self, db_path, failed, read_message):
        message_id = failed()

        assert retry.resume(db_path=db_path, message_id=message_id, guidance="try X")

        assert read_message(message_id)["status"] == db.STATUS_QUEUED

    def test_the_guidance_reaches_the_message(self, db_path, failed, read_message):
        message_id = failed()
        retry.resume(db_path=db_path, message_id=message_id, guidance="the fixtures are in tests/")

        parsed = handoff.parse_handoff(read_message(message_id)["content"])
        assert parsed.guidance == "the fixtures are in tests/"
        assert parsed.is_resume is True

    def test_the_original_handoff_survives_alongside_it(self, db_path, failed, read_message):
        # The worker needs the original brief as well as the correction.
        message_id = failed()
        retry.resume(db_path=db_path, message_id=message_id, guidance="try X")

        parsed = handoff.parse_handoff(read_message(message_id)["content"])
        assert parsed.sender == "specifier"
        assert parsed.handoff == "add-login"
        assert parsed.commit == "abc1234"

    def test_it_does_not_create_a_new_work_item(self, db_path, failed):
        message_id = failed()
        retry.resume(db_path=db_path, message_id=message_id, guidance="try X")

        assert db.count_work_item_arrivals(db_path, "add-login", "main", "coder") == 1
        assert message_id in [row["id"] for row in db.recent_messages(db_path, "main", limit=10)]

    def test_a_second_retry_replaces_the_first_guidance(self, db_path, failed, read_message):
        # Stacking would make the worker reconcile advice the human has already superseded.
        message_id = failed()
        retry.resume(db_path=db_path, message_id=message_id, guidance="first idea")
        db.mark_failed(db_path, message_id, "still stuck")
        retry.resume(db_path=db_path, message_id=message_id, guidance="second idea")

        content = read_message(message_id)["content"]
        assert "second idea" in content
        assert "first idea" not in content

    def test_resuming_something_that_did_not_fail_is_refused(self, db_path):
        message_id = db.insert_handoff(db_path, "specifier", "coder", "c", "main")
        assert retry.resume(db_path=db_path, message_id=message_id, guidance="x") is None

    def test_an_unknown_id_is_refused(self, db_path):
        assert retry.resume(db_path=db_path, message_id="nope", guidance="x") is None


class TestCli:
    def test_listing_shows_the_failure_reason(self, db_path, failed, capsys):
        failed(error="worker blocked: no fixtures")

        assert retry.main(["--db-path", str(db_path), "--branch", "main", "--list"]) == 0

        out = capsys.readouterr().out
        assert "worker blocked: no fixtures" in out
        assert "specifier -> coder" in out

    def test_no_arguments_lists_rather_than_failing(self, db_path, failed, capsys):
        # A human who has forgotten the id should get the id, not a usage error.
        failed()
        assert retry.main(["--db-path", str(db_path), "--branch", "main"]) == 0
        assert "failed message" in capsys.readouterr().out

    def test_an_empty_queue_says_so(self, db_path, capsys):
        assert retry.main(["--db-path", str(db_path), "--branch", "main"]) == 0
        assert "no failed messages" in capsys.readouterr().out

    def test_a_short_id_is_enough(self, db_path, failed, read_message, capsys):
        # The listing prints eight characters because a full uuid is unreadable; asking a
        # human to then type all 32 would make the listing useless.
        message_id = failed()

        code = retry.main(
            ["--db-path", str(db_path), "--branch", "main", message_id[:8], "--guidance", "x"]
        )

        assert code == 0
        assert read_message(message_id)["status"] == db.STATUS_QUEUED

    def test_an_ambiguous_prefix_is_refused(self, db_path, failed, capsys, monkeypatch):
        failed()
        shared = "aaaaaaaa"
        monkeypatch.setattr(
            db,
            "failed_messages",
            lambda *_a, **_k: [
                {
                    "id": shared + "1",
                    "error": "",
                    "work_item": "",
                    "sender": "s",
                    "target": "coder",
                    "created_at": "",
                },
                {
                    "id": shared + "2",
                    "error": "",
                    "work_item": "",
                    "sender": "s",
                    "target": "coder",
                    "created_at": "",
                },
            ],
        )

        assert retry.main(["--db-path", str(db_path), "--branch", "main", shared]) == 1
        assert "matches 2" in capsys.readouterr().err

    def test_an_unknown_prefix_is_refused(self, db_path, failed, capsys):
        failed()
        assert retry.main(["--db-path", str(db_path), "--branch", "main", "zzzzzzzz"]) == 1
        assert "no failed message" in capsys.readouterr().err

    def test_a_missing_queue_is_reported_plainly(self, tmp_path, capsys):
        missing = tmp_path / "nope.db"
        assert retry.main(["--db-path", str(missing), "--branch", "main", "abc"]) == 1
        assert "no message queue" in capsys.readouterr().err

    def test_retrying_without_guidance_warns(self, db_path, failed, capsys):
        # It will retry with exactly the brief that already failed, which is rarely intended.
        message_id = failed()
        retry.main(["--db-path", str(db_path), "--branch", "main", message_id[:8]])
        assert "no --guidance" in capsys.readouterr().out


class TestGuidanceFormatting:
    def test_guidance_is_absent_from_an_ordinary_message(self):
        assert handoff.parse_handoff("Sender: coder\n").guidance == ""
        assert handoff.parse_handoff("Sender: coder\n").is_resume is False

    def test_attaching_then_stripping_round_trips(self):
        original = "Sender: coder\nHandoff: x\n"
        attached = handoff.attach_guidance(original, "do it differently")
        assert handoff.strip_guidance(attached).rstrip() == original.rstrip()
