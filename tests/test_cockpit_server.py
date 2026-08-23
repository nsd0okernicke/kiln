"""
The cockpit's HTTP contract — a real server, over a real socket, against a real queue.

Issue #22's Phase 0 goal is "the browser shows the same numbers as the terminal dashboard
tab", so that is asserted directly: one fixture swarm, read through `dashboard.collect` and
through `GET /api/state`, and the two must agree.

Everything here binds port 0 (an ephemeral port), so the suite never collides with a cockpit
the developer happens to have running.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest
from cockpit import actions
from cockpit import state as cockpit_state
from cockpit.server import GUARD_HEADER, CockpitConfig, find_free_port, serve
from scheduler import dashboard, db
from scheduler.dashboard import DashboardContext

pytestmark = pytest.mark.integration


@pytest.fixture
def swarm(tmp_path, db_path):
    """A launched-looking project: sessions file, status directory, message queue."""
    state_dir = db_path.parent
    status_dir = state_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    sessions = state_dir / "sessions"
    sessions.write_text(
        "1\thuman-in-the-loop\tclaude\tHuman In The Loop\n"
        "2\tspecifier\tclaude\tSpecifier\n"
        "3\tcoder\tclaude\tCoder\n",
        encoding="utf-8",
    )
    (status_dir / "coder.json").write_text(
        json.dumps({
            "role": "coder", "state": "working", "since": "2026-08-09T14:59:30Z",
            "cycles": 2, "cost_usd": 1.25, "tokens": 1000,
            "token_usage": {"input": 400, "cache_read": 600},
        }),
        encoding="utf-8",
    )
    return {"db_path": db_path, "status_dir": status_dir, "sessions": sessions}


@pytest.fixture
def config(swarm):
    return CockpitConfig(
        dashboard=DashboardContext(
            db_path=swarm["db_path"],
            branch="main",
            status_dir=swarm["status_dir"],
            sessions_file=swarm["sessions"],
            project_name="demo",
        ),
        cockpit=cockpit_state.CockpitContext(
            project_name="demo", branch="main",
            lanes=("specifier", "coder"), intake_role="specifier",
        ),
        actions=actions.ActionContext(
            db_path=swarm["db_path"], branch="main", human_role="human-in-the-loop",
            intake_role="specifier", sessions_file=swarm["sessions"],
        ),
    )


@pytest.fixture
def client(config):
    """A live cockpit on an ephemeral port, torn down with the test."""
    server = serve(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class _Client:
        port = server.server_address[1]

        def request(self, method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            payload = json.dumps(body) if body is not None else None
            conn.request(method, path, payload, headers or {})
            response = conn.getresponse()
            raw = response.read()
            conn.close()
            try:
                return response.status, json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return response.status, raw

        def get(self, path):
            return self.request("GET", path, headers={GUARD_HEADER: "1"})

        def post(self, path, body, guarded=True):
            headers = {GUARD_HEADER: "1"} if guarded else {}
            return self.request("POST", path, body, headers)

    try:
        yield _Client()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestState:
    def test_the_browser_sees_what_the_dashboard_sees(self, client, config):
        # Phase 0's whole claim. Both go through `dashboard.collect`, so a divergence here
        # would mean the JSON builder reinterpreted a number rather than passing it on.
        snapshot = dashboard.collect(config.dashboard)
        expected = dashboard.render_totals(snapshot.statuses)

        _, payload = client.get("/api/state")

        assert payload["totals"]["cost_usd"] == expected[0]
        assert payload["totals"]["cycles"] == expected[1]
        assert payload["totals"]["tokens"] == expected[2]

    def test_it_lists_every_launched_role_in_profile_order(self, client):
        _, payload = client.get("/api/state")

        assert [row["role"] for row in payload["roles"]] == [
            "human-in-the-loop", "specifier", "coder",
        ]

    def test_a_queued_handoff_becomes_a_card_in_its_targets_lane(self, client, add_message):
        add_message(target="coder", work_item="ORDER-INTAKE", content="build it")

        _, payload = client.get("/api/state")

        assert [card["work_item"] for card in payload["board"]["cards"]["coder"]] == [
            "ORDER-INTAKE"
        ]

    def test_a_brand_new_request_is_visible_before_it_has_a_name(self, client, db_path):
        # End to end over HTTP, because this is the path that failed on a live run: the
        # request went into the queue with no work item, and the board -- which filtered
        # them out in SQL -- showed nothing for the eight minutes until the specifier
        # named it.
        add = db.insert_handoff(
            db_path, "human-in-the-loop", "specifier", "handoff the next userstory", "main",
        )

        _, payload = client.get("/api/state")

        cards = payload["board"]["cards"]["specifier"]
        assert [card["message_id"] for card in cards] == [add]
        assert cards[0]["unnamed"] is True

    def test_an_empty_project_still_answers(self, client):
        # The cockpit is often opened before anything has happened; a swarm with no messages
        # and no status files must render an empty board, not a 500.
        status, payload = client.get("/api/state")

        assert status == 200
        assert payload["board"]["lanes"] == ["specifier", "coder", "done"]

    def test_responses_are_never_cached(self, client, config):
        # A browser reusing a stored `/api/state` is a cockpit showing a run that has moved on.
        conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
        conn.request("GET", "/api/state", headers={GUARD_HEADER: "1"})
        response = conn.getresponse()
        response.read()
        conn.close()

        assert response.getheader("Cache-Control") == "no-store"


class TestPage:
    def test_the_root_serves_the_cockpit_page(self, client):
        status, body = client.get("/")

        assert status == 200
        assert b"Kiln Cockpit" in body

    def test_an_unknown_route_is_a_404_rather_than_a_crash(self, client):
        status, payload = client.get("/api/nonsense")

        assert status == 404
        assert "no route" in payload["error"]


class TestDocuments:
    def test_a_message_can_be_read_in_full(self, client, add_message):
        message_id = add_message(target="coder", content="the entire handoff body")

        status, payload = client.get(f"/api/messages/{message_id}")

        assert status == 200
        assert payload["content"] == "the entire handoff body"

    def test_an_unknown_message_is_a_404(self, client):
        status, _ = client.get("/api/messages/" + "0" * 32)

        assert status == 404

    def test_a_roles_raw_status_is_readable(self, client):
        status, payload = client.get("/api/status/coder")

        assert status == 200
        assert payload["state"] == "working"

    def test_a_role_this_swarm_never_launched_is_refused(self, client):
        status, _ = client.get("/api/status/attacker")

        assert status == 404

    def test_the_role_name_cannot_escape_the_status_directory(self, client):
        # The status endpoint builds a path from the URL, and this server has no
        # authentication of any kind: without the sessions-file check, `..` would be a
        # file-read primitive on the operator's machine.
        status, _ = client.get("/api/status/..%2F..%2Fsessions")

        assert status == 404


class TestGuardHeader:
    def test_a_mutating_request_without_the_guard_header_is_refused(self, client, db_path):
        status, payload = client.post(
            "/api/tasks", {"summary": "add order intake"}, guarded=False
        )

        assert status == 403
        assert GUARD_HEADER in payload["error"]
        assert db.recent_messages(db_path, "main") == []

    def test_a_refused_request_still_gets_its_body_read(self, client):
        # Windows resets a connection whose reply is written while unread request bytes are
        # still buffered, so a 403 sent without draining reaches the browser as a dropped
        # request rather than as the explanation it is. Found by exactly this test.
        status, _ = client.post("/api/tasks", {"summary": "x" * 5000}, guarded=False)

        assert status == 403

    def test_an_unroutable_post_is_refused_without_resetting_the_connection(self, client):
        status, payload = client.post("/api/nonsense", {"summary": "hello"})

        assert status == 404
        assert "no route" in payload["error"]


class TestSend:
    def test_it_queues_for_the_chosen_role(self, client, db_path):
        status, payload = client.post(
            "/api/send", {"target": "coder", "summary": "restart with CAT-3"}
        )

        assert status == 200
        assert payload["target"] == "coder"
        assert db.get_message(db_path, payload["message_id"])["target"] == "coder"

    def test_a_named_work_item_survives_the_round_trip(self, client, db_path):
        status, payload = client.post(
            "/api/send",
            {"target": "specifier", "summary": "restart", "work_item": "CAT-3"},
        )

        assert status == 200
        assert db.get_message(db_path, payload["message_id"])["work_item"] == "CAT-3"

    def test_an_unaddressable_target_is_a_400_naming_the_real_roles(self, client, db_path):
        status, payload = client.post("/api/send", {"target": "nosuchrole", "summary": "hi"})

        assert status == 400
        assert "specifier" in payload["error"]
        assert db.recent_messages(db_path, "main") == []


class TestTasks:
    def test_a_new_task_is_queued_for_the_intake_role(self, client, db_path):
        status, payload = client.post("/api/tasks", {"summary": "add order intake"})

        assert status == 200
        assert payload["target"] == "specifier"
        assert db.get_message(db_path, payload["message_id"])["target"] == "specifier"

    def test_an_empty_task_is_a_400_with_a_readable_reason(self, client):
        status, payload = client.post("/api/tasks", {"summary": ""})

        assert status == 400
        assert "task needs a description" in payload["error"]

    def test_a_chat_note_goes_to_the_human_role(self, client, db_path):
        status, payload = client.post("/api/chat", {"summary": "how is it going?"})

        assert status == 200
        assert db.get_message(db_path, payload["message_id"])["target"] == "human-in-the-loop"

    def test_a_body_that_is_not_json_is_refused(self, client):
        status, payload = client.request(
            "POST", "/api/tasks", headers={GUARD_HEADER: "1"}, body=None,
        )

        # No body at all is an empty object, which fails the same way an empty summary does.
        assert status == 400
        assert "description" in payload["error"]


class TestRetry:
    def test_a_failed_message_can_be_sent_back(self, client, db_path, add_message):
        message_id = add_message(target="coder", work_item="ALPHA")
        db.mark_failed(db_path, message_id, "worker gave up")

        status, payload = client.post(
            f"/api/retry/{message_id}", {"guidance": "read the spec first"}
        )

        assert status == 200
        assert payload["target"] == "coder"
        assert db.get_message(db_path, message_id)["status"] == db.STATUS_QUEUED

    def test_retrying_something_that_did_not_fail_is_a_400(self, client, add_message):
        message_id = add_message(target="coder")

        status, payload = client.post(f"/api/retry/{message_id}", {})

        assert status == 400
        assert "not a failed message" in payload["error"]


class TestTeardown:
    def test_it_refuses_without_the_confirmation_string(self, client, monkeypatch):
        stopped = []
        monkeypatch.setattr(actions.stop, "stop_all", lambda roles: stopped.append(roles))

        status, payload = client.post("/api/teardown", {})

        assert status == 400
        assert "TEARDOWN" in payload["error"]
        assert stopped == []

    def test_a_confirmed_teardown_answers_before_it_stops_anything(self, client, monkeypatch):
        # The order is the point: `stop_all` kills this very process, so a cockpit that tore
        # down inline would drop the connection and look indistinguishable from a crash at
        # the exact moment the operator needs to know what happened.
        done = threading.Event()
        monkeypatch.setattr(
            actions.stop, "stop_all", lambda roles: done.set() or [],
        )

        status, payload = client.post("/api/teardown", {"confirm": "TEARDOWN"})

        assert status == 200
        assert payload["stopping"] is True
        assert done.wait(timeout=5), "the deferred teardown never ran"


class TestPortSelection:
    def test_it_takes_the_preferred_port_when_it_is_free(self, client):
        # `client` is already listening on an ephemeral port, so the default is untouched.
        assert find_free_port(client.port + 1, attempts=1) == client.port + 1

    def test_a_taken_port_is_stepped_over_rather_than_failed_on(self, client):
        # Two projects open at once is normal. Binding blindly would leave the second
        # cockpit dead while its URL file still named a port — one that belongs to the
        # *other* project's swarm.
        assert find_free_port(client.port, attempts=5) != client.port
