"""Cockpit HTTP startup and configuration at process boundaries."""

from __future__ import annotations

import logging

import pytest

from kiln.cockpit.infrastructure.http import server
from kiln.cockpit.infrastructure.http.server import _log_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"stream": ["invalid"]}, "stream must be scheduler or worker"),
        ({"after": ["invalid"]}, "after must be a non-negative integer"),
        ({"after": ["-2"]}, ("scheduler", 0)),
    ],
)
def test_log_query_validation(query, expected):
    assert _log_query(query) == expected


def required_args(tmp_path, *extra: str) -> list[str]:
    return [
        "--db-path",
        str(tmp_path / "messages.db"),
        "--status-dir",
        str(tmp_path / "status"),
        "--sessions-file",
        str(tmp_path / "sessions"),
        *extra,
    ]


def test_config_from_args_normalizes_lanes_paths_and_project_name(tmp_path):
    args = server.build_parser().parse_args(
        required_args(
            tmp_path,
            "--branch",
            "feature",
            "--project-name",
            "demo",
            "--lanes",
            " specifier, coder, ,reviewer ",
            "--traffic-db",
            str(tmp_path / "traffic.db"),
            "--activity-limit",
            "7",
            "--intake-role",
            "specifier",
        )
    )
    config = server.config_from_args(args)
    assert config.dashboard.branch == "feature"
    assert config.dashboard.traffic_db == tmp_path / "traffic.db"
    assert config.cockpit.lanes == ("specifier", "coder", "reviewer")
    assert config.cockpit.project_name == "demo"
    assert config.actions.gateway is not None
    assert config.activity_limit == 7


def test_config_uses_current_directory_name_when_project_name_is_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = server.config_from_args(server.build_parser().parse_args(required_args(tmp_path)))
    assert config.cockpit.project_name == tmp_path.name
    assert config.dashboard.project_name == tmp_path.name


def test_launch_discovery_files_contain_url_and_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(server.os, "getpid", lambda: 4321)
    url_file, pid_file = tmp_path / "state" / "url", tmp_path / "state" / "pid"

    server.write_launch_files("http://127.0.0.1:9000", url_file, pid_file)

    assert url_file.read_text(encoding="utf-8") == "http://127.0.0.1:9000\n"
    assert pid_file.read_text(encoding="utf-8") == "4321\n"


def test_unwritable_discovery_file_warns_without_aborting(tmp_path, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        server.write_launch_files("http://cockpit", blocker / "url", None)

    assert "could not write" in caplog.text


def test_browser_opt_out_environment_prevents_thread_creation(monkeypatch):
    monkeypatch.setenv(server.NO_BROWSER_ENV, "1")
    monkeypatch.setattr(
        server.threading, "Thread", lambda *args, **kwargs: pytest.fail("thread created")
    )
    assert server.open_browser("http://cockpit") is False


def test_browser_is_opened_on_a_daemon_thread(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            started.append("started")

    monkeypatch.delenv(server.NO_BROWSER_ENV, raising=False)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    assert server.open_browser("http://cockpit") is True
    assert started[-1] == "started"
    assert started[0][1] == ("http://cockpit",)
    assert started[0][2] is True


class FakeServer:
    server_address = ("127.0.0.1", 9123)

    def __init__(self, interrupt: bool = False):
        self.interrupt = interrupt
        self.served = False
        self.closed = False

    def serve_forever(self):
        self.served = True
        if self.interrupt:
            raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def wire_main(monkeypatch, fake):
    seen = {}
    monkeypatch.setattr(server, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "find_free_port", lambda port: port + 2)
    monkeypatch.setattr(
        server, "serve", lambda config, port: seen.update(config=config, port=port) or fake
    )
    monkeypatch.setattr(
        server,
        "write_launch_files",
        lambda url, url_file, pid_file: seen.update(url=url, url_file=url_file, pid_file=pid_file),
    )
    return seen


def test_main_selects_port_writes_discovery_files_and_opens_browser(tmp_path, monkeypatch, capsys):
    fake = FakeServer()
    seen = wire_main(monkeypatch, fake)
    opened = []
    monkeypatch.setattr(server, "open_browser", opened.append)
    result = server.main(
        required_args(
            tmp_path,
            "--port",
            "9000",
            "--url-file",
            str(tmp_path / "url"),
            "--pid-file",
            str(tmp_path / "pid"),
        )
    )
    assert result == 0
    assert seen["port"] == 9002
    assert seen["url"] == "http://127.0.0.1:9123"
    assert opened == [seen["url"]]
    assert fake.served and fake.closed
    assert "Kiln cockpit" in capsys.readouterr().out


def test_main_keeps_ephemeral_port_and_honors_no_browser(tmp_path, monkeypatch):
    fake = FakeServer()
    seen = wire_main(monkeypatch, fake)
    monkeypatch.setattr(
        server, "find_free_port", lambda port: pytest.fail("port zero must not be probed")
    )
    monkeypatch.setattr(server, "open_browser", lambda url: pytest.fail("browser opened"))
    assert server.main(required_args(tmp_path, "--port", "0", "--no-browser")) == 0
    assert seen["port"] == 0


def test_keyboard_interrupt_closes_server_and_returns_shell_interrupt_code(tmp_path, monkeypatch):
    fake = FakeServer(interrupt=True)
    wire_main(monkeypatch, fake)
    monkeypatch.setattr(server, "open_browser", lambda url: None)
    assert server.main(required_args(tmp_path, "--no-browser")) == 130
    assert fake.closed
