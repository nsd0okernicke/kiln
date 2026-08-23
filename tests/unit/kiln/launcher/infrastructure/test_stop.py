"""Unit coverage for platform process-list parsing and stop orchestration."""

from kiln.launcher.infrastructure import stop


def test_parse_windows_process_lines_skips_headers_blanks_and_bad_pids():
    output = "\nProcessId\tCommandLine\n42\tpython -m kiln.proxy.infrastructure.http.server\n"

    assert stop._parse_lines(output, separator="\t") == [
        (42, "python -m kiln.proxy.infrastructure.http.server")
    ]


def test_parse_posix_process_lines_preserves_the_complete_command():
    assert stop._parse_lines("  17 python -m kiln.scheduler.infrastructure.cli.inbox\n", None) == [
        (17, "python -m kiln.scheduler.infrastructure.cli.inbox")
    ]


def test_stop_all_dry_run_reports_processes_without_killing(monkeypatch):
    killed = []
    tmux = []
    monkeypatch.setattr(stop, "find_kiln_processes", lambda: [(11, "kiln worker")])
    monkeypatch.setattr(stop, "kill_process", killed.append)
    monkeypatch.setattr(stop, "kill_tmux_sessions", lambda roles: tmux.append(roles))

    assert stop.stop_all(["coder"], dry_run=True) == [11]
    assert killed == []
    assert tmux == []


def test_stop_all_kills_processes_and_named_tmux_sessions(monkeypatch):
    killed = []
    monkeypatch.setattr(stop, "find_kiln_processes", lambda: [(11, "kiln worker")])
    monkeypatch.setattr(stop, "kill_process", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(stop, "kill_tmux_sessions", lambda roles: 1)

    assert stop.stop_all(["coder"]) == [11]
    assert killed == [11]
