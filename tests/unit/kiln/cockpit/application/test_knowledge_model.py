from __future__ import annotations

from datetime import datetime, timedelta

from kiln.cockpit.application import knowledge

NOW = datetime(2026, 10, 5, 12, 0, 0)


def payload(**overrides) -> dict:
    base = {
        "configured": True,
        "vault": "C:/vaults/kb",
        "exclude": ["01 Inbox"],
        "documents": 42,
        "source": {"status": "indexed", "error": None},
        "last_run": {
            "finished_at": (NOW - timedelta(minutes=5)).isoformat(),
            "updated": 2,
            "skipped": 40,
            "removed": 0,
            "failed": 0,
        },
    }
    return {**base, **overrides}


class TestBuildKnowledge:
    def test_a_synced_vault_is_indexed_with_an_age(self):
        document = knowledge.build_knowledge(payload(), NOW)

        assert document["status"] == "indexed"
        assert document["notes"] == 42
        assert document["synced_ago"] == "5m ago"
        assert document["vault"] == "C:/vaults/kb"
        assert document["error"] is None

    def test_a_vault_that_never_synced_is_never_synced(self):
        document = knowledge.build_knowledge(payload(last_run=None, source={}), NOW)

        assert document["status"] == "never-synced"
        assert document["synced_ago"] is None

    def test_a_failed_sync_carries_the_error(self):
        document = knowledge.build_knowledge(
            payload(source={"status": "failed", "error": "vault not found: C:/gone"}), NOW
        )

        assert document["status"] == "failed"
        assert document["error"] == "vault not found: C:/gone"

    def test_an_unconfigured_vault_is_ordinary_state_not_an_error(self):
        document = knowledge.build_knowledge({"configured": False}, NOW)

        assert document["status"] == "not-configured"
        assert document["notes"] == 0
        assert "vault" in document["error"]

    def test_an_unparseable_synced_at_yields_no_age(self):
        document = knowledge.build_knowledge(payload(last_run={"finished_at": "not a date"}), NOW)

        assert document["synced_ago"] is None


class TestUnavailable:
    def test_an_unreadable_config_names_the_fault(self):
        document = knowledge.unavailable("could not read knowledge config x")

        assert document["status"] == "unavailable"
        assert document["configured"] is False
        assert document["error"]
