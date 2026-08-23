"""Unit coverage for platform process-list parsing and stop orchestration."""

from types import SimpleNamespace

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


def test_find_kiln_processes_uses_windows_inventory_and_filters_markers(monkeypatch):
    monkeypatch.setattr(stop.os, "name", "nt")
    monkeypatch.setattr(
        stop,
        "_windows_matches",
        lambda: [
            (1, "python unrelated.py"),
            (2, "python -m kiln.scheduler.infrastructure.cli.inbox"),
        ],
    )
    monkeypatch.setattr(
        stop, "_posix_matches", lambda: (_ for _ in ()).throw(AssertionError("posix called"))
    )

    assert stop.find_kiln_processes() == [(2, "python -m kiln.scheduler.infrastructure.cli.inbox")]


def test_find_kiln_processes_uses_posix_inventory(monkeypatch):
    monkeypatch.setattr(stop.os, "name", "posix")
    monkeypatch.setattr(
        stop,
        "_posix_matches",
        lambda: [(3, "python -m kiln.cockpit.infrastructure.http.server")],
    )
    assert stop.find_kiln_processes() == [(3, "python -m kiln.cockpit.infrastructure.http.server")]


def test_kill_process_reports_posix_success_and_failure(monkeypatch):
    monkeypatch.setattr(stop.os, "name", "posix")
    monkeypatch.setattr(stop.os, "kill", lambda pid, signal: None)
    assert stop.kill_process(12) is True

    monkeypatch.setattr(stop.os, "kill", lambda pid, signal: (_ for _ in ()).throw(OSError("gone")))
    assert stop.kill_process(12) is False


def test_kill_process_uses_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(stop.os, "name", "nt")
    monkeypatch.setattr(
        stop.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    assert stop.kill_process(12) is True


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


def test_tmux_cleanup_is_skipped_when_binary_is_absent(monkeypatch):
    monkeypatch.setattr(stop.shutil, "which", lambda name: None)
    assert stop.kill_tmux_sessions(["coder"]) == 0


def test_tmux_cleanup_counts_only_sessions_killed_successfully(monkeypatch):
    calls = []
    results = iter((0, 1))
    monkeypatch.setattr(stop.shutil, "which", lambda name: "tmux")
    monkeypatch.setattr(
        stop.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or SimpleNamespace(returncode=next(results))
        ),
    )

    assert stop.kill_tmux_sessions(["human", "coder"]) == 1
    assert calls == [
        ["tmux", "kill-session", "-t", "kiln-human"],
        ["tmux", "kill-session", "-t", "kiln-coder"],
    ]
