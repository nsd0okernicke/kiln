"""
`GET /api/test-metrics` over a real socket (issue #27).

The contract this file defends is the isolation one: the test-metrics endpoint is separate
from `/api/state` precisely so an unreadable report can only spoil its own panel. So the
central assertion is not "the numbers are right" -- `test_test_reports.py` covers that -- but
"`/api/state` still answers 200 when the report is garbage".

Ephemeral ports throughout, so the suite never collides with a running cockpit.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from kiln.cockpit.application import actions, test_metrics
from kiln.cockpit.application import state as cockpit_state
from kiln.cockpit.infrastructure import test_reports
from kiln.cockpit.infrastructure.actions_gateway import KilnActionGateway
from kiln.cockpit.infrastructure.http.server import (
    GUARD_HEADER,
    CockpitConfig,
    build_parser,
    config_from_args,
    serve,
)
from kiln.scheduler.infrastructure.cli.dashboard import DashboardContext

pytestmark = pytest.mark.integration

SUITE = '<testsuite name="s" tests="4" failures="0" errors="0" skipped="1" time="2.5"/>'


@pytest.fixture
def project(tmp_path, db_path):
    """A launched-looking project whose reports live where its config says they do."""
    state_dir = db_path.parent
    status_dir = state_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    sessions = state_dir / "sessions"
    sessions.write_text("1\tcoder\tclaude\tCoder\n", encoding="utf-8")
    (tmp_path / "junit.xml").write_text(SUITE, encoding="utf-8")
    config_file = test_reports.config_path(tmp_path)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        json.dumps({"framework": "pytest", "reports": {"junit": "junit.xml"}}), encoding="utf-8"
    )
    return {
        "root": tmp_path,
        "db_path": db_path,
        "status_dir": status_dir,
        "sessions": sessions,
    }


def make_config(project, metrics_path):
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
        test_metrics_path=metrics_path,
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


class TestEndpoint:
    def test_serves_the_configured_report(self, project, client_for):
        get = client_for(make_config(project, test_reports.config_path(project["root"])))
        status, payload = get("/api/test-metrics")
        assert status == 200
        assert payload["status"] == test_metrics.STATUS_PASSED
        assert (payload["tests"], payload["skipped"]) == (4, 1)
        assert payload["source"] == "pytest"

    def test_an_unconfigured_project_gets_an_explanation_not_a_404(self, project, client_for):
        """
        The panel must be able to tell "not configured" from "not reporting", so this is a
        200 with a sentence rather than a missing route.
        """
        get = client_for(make_config(project, project["root"] / "absent.json"))
        status, payload = get("/api/test-metrics")
        assert status == 200
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert payload["error"]

    def test_a_config_written_after_launch_is_picked_up_without_a_restart(
        self, project, client_for
    ):
        """
        The reason the path is stored rather than the parsed config. Setting the feature up
        is exactly when the file appears *after* the cockpit started — and needing a restart
        there is indistinguishable from the feature not working.
        """
        path = test_reports.config_path(project["root"])
        path.unlink()
        get = client_for(make_config(project, path))
        assert get("/api/test-metrics")[1]["status"] == test_metrics.STATUS_UNAVAILABLE

        path.write_text(
            json.dumps({"framework": "pytest", "reports": {"junit": "junit.xml"}}),
            encoding="utf-8",
        )

        assert get("/api/test-metrics")[1]["status"] == test_metrics.STATUS_PASSED

    def test_a_broken_config_says_so_instead_of_asking_for_one(self, project, client_for):
        path = test_reports.config_path(project["root"])
        path.write_text("{not json", encoding="utf-8")

        payload = client_for(make_config(project, path))("/api/test-metrics")[1]

        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert "unreadable" in payload["error"]
        assert "create one" not in payload["error"]

    def test_a_trailing_slash_reaches_the_same_route(self, project, client_for):
        get = client_for(make_config(project, test_reports.config_path(project["root"])))
        assert get("/api/test-metrics/")[0] == 200


class TestIsolationFromState:
    def test_a_malformed_report_does_not_break_the_state_document(self, project, client_for):
        """
        The reason this is a separate endpoint at all. An unparseable XML file must not put
        the board, the queue and the attention rail on the floor.
        """
        (project["root"] / "junit.xml").write_text("<testsuite", encoding="utf-8")
        get = client_for(make_config(project, test_reports.config_path(project["root"])))

        metrics_status, metrics = get("/api/test-metrics")
        state_status, state = get("/api/state")

        assert metrics_status == 200
        assert metrics["status"] == test_metrics.STATUS_UNAVAILABLE
        assert state_status == 200
        assert state["roles"]

    def test_a_missing_report_does_not_break_the_state_document(self, project, client_for):
        (project["root"] / "junit.xml").unlink()
        get = client_for(make_config(project, test_reports.config_path(project["root"])))
        assert get("/api/test-metrics")[1]["status"] == test_metrics.STATUS_UNAVAILABLE
        assert get("/api/state")[0] == 200


class TestArgumentWiring:
    def _args(self, project, extra):
        return build_parser().parse_args(
            [
                "--db-path",
                str(project["db_path"]),
                "--status-dir",
                str(project["status_dir"]),
                "--sessions-file",
                str(project["sessions"]),
                *extra,
            ]
        )

    def test_project_root_makes_the_config_discoverable(self, project):
        """What the launcher passes: a root, and the config is found inside it."""
        config = config_from_args(self._args(project, ["--project-root", str(project["root"])]))
        assert config.test_metrics_path == test_reports.config_path(project["root"])

    def test_an_explicit_config_path_wins(self, project, tmp_path):
        elsewhere = tmp_path / "custom.json"
        elsewhere.write_text(
            json.dumps({"framework": "gradle", "reports": {"junit": "build/results"}}),
            encoding="utf-8",
        )
        config = config_from_args(
            self._args(
                project, ["--project-root", str(project["root"]), "--test-metrics", str(elsewhere)]
            )
        )
        assert config.test_metrics_path == elsewhere

    def test_a_project_without_the_file_still_gets_a_path_to_watch(self, project, tmp_path):
        """
        The path is resolved even when nothing is there yet, because the endpoint re-reads it
        on every poll — a config written after launch has to be picked up without a restart.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        config = config_from_args(self._args(project, ["--project-root", str(empty)]))
        assert config.test_metrics_path == test_reports.config_path(empty)
