"""
`GET /api/knowledge` over a real socket.

The contract this file defends is the isolation one: the knowledge endpoint is separate from
`/api/state` precisely so a project with no vault configured can only spoil its own panel.
So the central assertion is not "the numbers are right" -- the builder's unit tests cover
that -- but "the endpoint answers 200 with a sentence for every ordinary state".

Ephemeral ports throughout, so the suite never collides with a running cockpit.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from kiln.cockpit.application import actions, knowledge
from kiln.cockpit.application import state as cockpit_state
from kiln.cockpit.infrastructure.actions_gateway import KilnActionGateway
from kiln.cockpit.infrastructure.http.server import GUARD_HEADER, CockpitConfig, serve
from kiln.scheduler.infrastructure.cli.dashboard import DashboardContext

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path, db_path):
    """A launched-looking project whose knowledge config lives where launch writes it."""
    state_dir = db_path.parent
    status_dir = state_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    sessions = state_dir / "sessions"
    sessions.write_text("1\tcoder\tclaude\tCoder\n", encoding="utf-8")
    config = tmp_path / "kiln" / "project" / "knowledge.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"version": 2, "vault": "", "exclude": []}\n', encoding="utf-8")
    return {
        "root": tmp_path,
        "db_path": db_path,
        "status_dir": status_dir,
        "sessions": sessions,
        "config": config,
    }


def make_config(project):
    return CockpitConfig(
        dashboard=DashboardContext(
            db_path=project["db_path"],
            branch="main",
            status_dir=project["status_dir"],
            sessions_file=project["sessions"],
            project_name="demo",
        ),
        cockpit=cockpit_state.CockpitContext(project_name="demo", branch="main"),
        actions=actions.ActionContext(
            db_path=project["db_path"],
            branch="main",
            human_role="human-in-the-loop",
            intake_role="specifier",
            sessions_file=project["sessions"],
            gateway=KilnActionGateway(),
        ),
        project_root=project["root"],
    )


@pytest.fixture
def client_for():
    """Serve a given config on an ephemeral port; every server is shut down afterwards."""
    started = []

    def _serve(config):
        server = serve(config, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", path, None, {GUARD_HEADER: "1"})
            response = conn.getresponse()
            raw = response.read()
            conn.close()
            return response.status, json.loads(raw)

        return get

    yield _serve
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestKnowledgeEndpoint:
    def test_an_unconfigured_project_gets_an_explanation_not_a_404(self, project, client_for):
        get = client_for(make_config(project))
        status, payload = get("/api/knowledge")

        assert status == 200
        assert payload["status"] == knowledge.STATUS_NOT_CONFIGURED
        assert payload["error"]

    def test_serves_a_synced_vault(self, project, client_for):
        vault = project["root"] / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("# Note\nbody text", encoding="utf-8")
        project["config"].write_text(
            json.dumps({"version": 2, "vault": str(vault), "exclude": []}), encoding="utf-8"
        )
        from kiln.knowledge.infrastructure.factory import knowledge_service

        with knowledge_service(project["root"]) as service:
            service.sync()

        status, payload = client_for(make_config(project))("/api/knowledge")

        assert status == 200
        assert payload["status"] == knowledge.STATUS_INDEXED
        assert payload["notes"] == 1
        assert payload["synced_ago"]

    def test_a_vault_written_after_launch_is_picked_up_without_a_restart(self, project, client_for):
        get = client_for(make_config(project))
        assert get("/api/knowledge")[1]["status"] == knowledge.STATUS_NOT_CONFIGURED

        vault = project["root"] / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("body", encoding="utf-8")
        project["config"].write_text(
            json.dumps({"version": 2, "vault": str(vault), "exclude": []}), encoding="utf-8"
        )

        payload = get("/api/knowledge")[1]

        assert payload["status"] == knowledge.STATUS_NEVER_SYNCED
        assert payload["configured"] is True
