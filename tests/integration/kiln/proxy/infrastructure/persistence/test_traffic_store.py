"""Integration tests for the SQLite traffic-store adapter."""

from __future__ import annotations

import json
import sqlite3

from kiln.proxy.domain.capture import (
    BODY_BUDGET_CHECK_EVERY,
    CaptureMode,
    TrafficRecord,
    capture_body,
    extract_composition,
)
from kiln.proxy.infrastructure.persistence import TrafficStore
from kiln.scheduler.domain.models import TokenUsage


class TestTrafficStore:
    def _store(self, tmp_path):
        store = TrafficStore(tmp_path / "traffic.db")
        store.ensure_schema()
        return store

    def test_creates_its_own_file_not_messages_db(self, tmp_path):
        # messages.db is live swarm state read by the dashboard, the inbox and the kiln-db
        # MCP server; request bodies have no business in it.
        self._store(tmp_path)
        assert (tmp_path / "traffic.db").is_file()
        assert not (tmp_path / "messages.db").exists()

    def test_ensure_schema_is_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        store.ensure_schema()  # must not raise

    def test_an_older_store_gains_the_new_columns(self, tmp_path):
        """
        `CREATE TABLE IF NOT EXISTS` never alters an existing table.

        Without a migration, a store created before the composition columns existed keeps
        the old shape and every insert naming them fails -- the same trap
        `scheduler.db.ensure_schema` documents for the message queue.
        """
        import sqlite3

        path = tmp_path / "traffic.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                " role TEXT, method TEXT NOT NULL, path TEXT NOT NULL, model TEXT,"
                " status_code INTEGER, duration_ms INTEGER,"
                " request_bytes INTEGER NOT NULL DEFAULT 0,"
                " response_bytes INTEGER NOT NULL DEFAULT 0,"
                " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
                " cache_creation_tokens INTEGER, request_headers TEXT, request_body TEXT,"
                " response_body TEXT)"
            )
            conn.execute(
                "INSERT INTO traffic (ts, role, method, path, request_bytes) "
                "VALUES ('2026-08-13T07:00:00Z', 'coder', 'POST', '/v1/messages', 500)"
            )

        store = TrafficStore(path)
        store.ensure_schema()
        store.record(
            TrafficRecord(
                role="coder",
                method="POST",
                path="/v1/messages",
                composition={"tools": 10, "system": 20, "messages": 30},
            )
        )

        stats = store.request_stats_by_role()["coder"]
        assert stats["requests"] == 2, "the pre-existing row must survive the migration"
        assert stats["avg_tools"] == 10  # averaged over the row that has data

    def test_records_an_exchange(self, tmp_path):
        store = self._store(tmp_path)
        row_id = store.record(
            TrafficRecord(
                role="coder",
                method="POST",
                path="/v1/messages",
                status_code=200,
                request_bytes=120,
                response_bytes=4096,
                model="claude-sonnet-5",
                tokens=TokenUsage(input_tokens=100, cache_read_tokens=900),
            )
        )
        assert row_id > 0

    def test_totals_are_summed_per_role(self, tmp_path):
        store = self._store(tmp_path)
        for role, tokens in (
            ("coder", TokenUsage(input_tokens=10, cache_read_tokens=100)),
            ("coder", TokenUsage(input_tokens=5)),
            ("refactorer", TokenUsage(output_tokens=7)),
        ):
            store.record(
                TrafficRecord(role=role, method="POST", path="/v1/messages", tokens=tokens)
            )
        totals = store.totals_by_role()
        assert totals["coder"] == TokenUsage(input_tokens=15, cache_read_tokens=100)
        assert totals["refactorer"] == TokenUsage(output_tokens=7)

    def test_unattributed_traffic_is_excluded_from_totals(self, tmp_path):
        store = self._store(tmp_path)
        store.record(
            TrafficRecord(
                role=None, method="POST", path="/v1/messages", tokens=TokenUsage(input_tokens=99)
            )
        )
        assert store.totals_by_role() == {}

    def test_request_stats_summarise_prompt_weight_per_role(self, tmp_path):
        store = self._store(tmp_path)
        for size in (100, 300):
            store.record(
                TrafficRecord(role="coder", method="POST", path="/v1/messages", request_bytes=size)
            )
        stats = store.request_stats_by_role()["coder"]
        assert stats["requests"] == 2
        assert stats["avg_bytes"] == 200
        assert stats["max_bytes"] == 300
        assert stats["total_bytes"] == 400

    def test_request_stats_are_empty_before_any_traffic(self, tmp_path):
        assert self._store(tmp_path).request_stats_by_role() == {}

    def test_request_stats_on_a_missing_store_are_empty(self, tmp_path):
        assert TrafficStore(tmp_path / "absent.db").request_stats_by_role() == {}

    def test_a_store_without_the_composition_columns_still_reports_sizes(self, tmp_path):
        """
        Degrade per column, not per panel.

        Observed live: naming `tools_bytes` against a store written by a proxy that
        predated it raised, the blanket except swallowed it, and the entire prompt-weight
        panel vanished from the dashboard -- taking the request-size figures with it, which
        need nothing new.
        """
        import sqlite3

        path = tmp_path / "traffic.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,"
                " role TEXT, method TEXT, path TEXT, request_bytes INTEGER)"
            )
            conn.execute(
                "INSERT INTO traffic (ts, role, method, path, request_bytes) "
                "VALUES ('2026-08-13T07:00:00Z', 'coder', 'POST', '/v1/messages', 1500)"
            )

        stats = TrafficStore(path).request_stats_by_role()["coder"]
        assert stats["requests"] == 1
        assert stats["avg_bytes"] == 1500  # still available
        assert stats["avg_tools"] is None  # honestly absent

    def test_request_stats_on_an_unreadable_store_are_empty(self, tmp_path):
        # An optional panel must not be able to take the dashboard down.
        junk = tmp_path / "traffic.db"
        junk.write_text("not a database", encoding="utf-8")
        assert TrafficStore(junk).request_stats_by_role() == {}

    def test_composition_is_recorded_and_averaged(self, tmp_path):
        store = self._store(tmp_path)
        for tools in (1000, 3000):
            store.record(
                TrafficRecord(
                    role="coder",
                    method="POST",
                    path="/v1/messages",
                    request_bytes=10_000,
                    composition={"tools": tools, "system": 500, "messages": 4000},
                )
            )
        stats = store.request_stats_by_role()["coder"]
        assert stats["avg_tools"] == 2000
        assert stats["avg_system"] == 500
        assert stats["avg_messages"] == 4000

    def test_composition_survives_metadata_mode(self, tmp_path):
        # The point of computing it at capture time: the most useful analysis costs nothing
        # in stored prompt text.
        store = self._store(tmp_path)
        secret = json.dumps(
            {
                "tools": [{"name": "Read"}],
                "system": "PROPRIETARY",
                "messages": [{"content": "SECRET SOURCE"}],
            }
        )
        store.record(
            TrafficRecord(
                role="coder",
                method="POST",
                path="/v1/messages",
                composition=extract_composition(secret),
                request_body=capture_body(secret, CaptureMode.METADATA),
            )
        )
        assert store.request_stats_by_role()["coder"]["avg_system"] > 0
        assert "SECRET SOURCE" not in (tmp_path / "traffic.db").read_bytes().decode("latin-1")

    def test_rows_without_composition_report_none_not_zero(self, tmp_path):
        store = self._store(tmp_path)
        store.record(TrafficRecord(role="coder", method="POST", path="/v1/messages"))
        assert store.request_stats_by_role()["coder"]["avg_tools"] is None

    def test_metadata_mode_leaves_no_prompt_text_on_disk(self, tmp_path):
        store = self._store(tmp_path)
        secret = "PROPRIETARY SOURCE CODE"
        store.record(
            TrafficRecord(
                role="coder",
                method="POST",
                path="/v1/messages",
                request_body=capture_body(secret, CaptureMode.METADATA),
                response_body=capture_body(secret, CaptureMode.METADATA),
            )
        )
        assert secret not in (tmp_path / "traffic.db").read_bytes().decode("latin-1")


class TestBodyBudget:
    """
    Retention, so a long capture run cannot silently fill the disk.

    Bodies are what grows -- 98.3% of a real 107.6MB store -- and they are also the part a
    row does not need to stay useful, because composition and usage are computed at capture
    time. So the budget clears bodies rather than deleting rows.
    """

    def _store(self, tmp_path, budget):
        store = TrafficStore(tmp_path / "traffic.db", body_budget=budget)
        store.ensure_schema()
        return store

    def _record(self, store, body, role="coder"):
        return store.record(
            TrafficRecord(
                role=role,
                method="POST",
                path="/v1/messages",
                composition={"tools": 10, "system": 20, "messages": 30},
                tokens=TokenUsage(input_tokens=5, output_tokens=1),
                request_body=body,
            )
        )

    def test_a_store_inside_its_budget_is_untouched(self, tmp_path):
        store = self._store(tmp_path, budget=10_000)
        self._record(store, "x" * 100)
        assert store.enforce_body_budget() == 0

    def test_the_oldest_bodies_go_first(self, tmp_path):
        store = self._store(tmp_path, budget=250)
        for _ in range(4):
            self._record(store, "x" * 100)
        store.enforce_body_budget()
        with sqlite3.connect(tmp_path / "traffic.db") as conn:
            kept = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM traffic WHERE request_body IS NOT NULL ORDER BY id"
                )
            ]
        # 400 bytes stored against a 250 budget: the two oldest are cleared, not the newest.
        assert kept == [3, 4]

    def test_rows_are_degraded_never_deleted(self, tmp_path):
        store = self._store(tmp_path, budget=50)
        for _ in range(3):
            self._record(store, "x" * 100)
        store.enforce_body_budget()
        with sqlite3.connect(tmp_path / "traffic.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM traffic").fetchone()[0] == 3

    def test_a_degraded_row_keeps_everything_the_dashboard_reads(self, tmp_path):
        # The whole reason for clearing bodies instead of dropping rows.
        store = self._store(tmp_path, budget=50)
        for _ in range(3):
            self._record(store, "x" * 100)
        store.enforce_body_budget()
        stats = store.request_stats_by_role()["coder"]
        assert stats["requests"] == 3
        assert stats["avg_tools"] == 10
        assert stats["avg_messages"] == 30

    def test_a_metadata_only_store_never_trips_it(self, tmp_path):
        # It writes no bodies at all, so the sum stays at zero however long the run is.
        store = self._store(tmp_path, budget=1)
        for _ in range(5):
            self._record(store, capture_body("SECRET", CaptureMode.METADATA))
        assert store.enforce_body_budget() == 0

    def test_a_disabled_budget_does_nothing(self, tmp_path):
        store = self._store(tmp_path, budget=0)
        self._record(store, "x" * 1000)
        assert store.enforce_body_budget() == 0

    def test_record_enforces_it_periodically_without_being_asked(self, tmp_path):
        # A long run must stay bounded on its own; nothing else calls enforce_body_budget().
        #
        # The budget is a ceiling checked every BODY_BUDGET_CHECK_EVERY writes, not a hard
        # cap applied to each one, so the store can sit one check-interval above it. That is
        # the trade the interval buys: the check is a full-table SUM.
        writes = BODY_BUDGET_CHECK_EVERY + 1
        store = self._store(tmp_path, budget=500)
        for _ in range(writes):
            self._record(store, "x" * 100)
        with sqlite3.connect(tmp_path / "traffic.db") as conn:
            stored = conn.execute(
                "SELECT COALESCE(SUM(length(coalesce(request_body,''))), 0) FROM traffic"
            ).fetchone()[0]
        assert stored < writes * 100  # unbounded growth would keep every one
        assert stored <= 500 + BODY_BUDGET_CHECK_EVERY * 100
