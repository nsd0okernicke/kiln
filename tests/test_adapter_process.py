"""
Killing a worker means killing what it spawned.

This is not a hypothetical. A codex coder ran `pwsh -Command 'uv run pytest tests/acceptance'`
against a project whose fixtures start a PostgreSQL container, on a machine with no Docker
daemon. Testcontainers waits on the daemon rather than failing, so the run stopped instead of
erroring. The watchdog fired at 1800s and called `process.kill()` -- which reached only the
`codex` process. `pwsh`, `uv` and `pytest` lived on holding the stdout pipe they had
inherited, so the adapter's reader loop stayed blocked. The handoff was released at **2698s
against an 1800s cap**, and the first attempt of the same message released on time, so the
overrun is not a constant to budget for: it is however long the deepest survivor takes.

The tests below pin the property that actually matters -- the read completes -- rather than
the mechanism, because the mechanism differs per platform (`taskkill /T` vs `killpg`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest
from scheduler.adapters import REAP_TIMEOUT_SEC, terminate_tree

#: Long enough that nothing finishes on its own during a test, so a passing assertion can
#: only mean the kill worked.
SLEEP_SEC = 120

#: A parent that spawns a child inheriting its stdout, announces itself, then idles. The
#: grandchild is the process `Popen.kill()` cannot reach.
SPAWNER = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({SLEEP_SEC})']); "
    "print('spawned', flush=True); "
    f"time.sleep({SLEEP_SEC})"
)


def _spawn_tree() -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", SPAWNER],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,  # what makes the POSIX group signal safe
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "spawned", "child never started"
    return process


def _read_completes_within(process: subprocess.Popen, seconds: float) -> bool:
    """
    True if draining stdout finishes -- which it can only do once *every* writer is gone.

    Done on a daemon thread rather than inline: if the grandchild survives there is no read
    timeout to fall back on, and a failing assertion is worth more than a hung suite.
    """
    finished = threading.Event()

    def _drain() -> None:
        assert process.stdout is not None
        process.stdout.read()
        finished.set()

    threading.Thread(target=_drain, daemon=True).start()
    return finished.wait(seconds)


class TestTerminateTree:
    def test_it_unblocks_a_read_held_open_by_a_grandchild(self):
        # The regression itself. `process.kill()` here leaves the grandchild holding the
        # pipe, and this read never returns.
        process = _spawn_tree()
        try:
            terminate_tree(process)
            assert _read_completes_within(process, 15), (
                "stdout still has a writer, so a descendant outlived the kill — this is the "
                "2698s-against-1800s overrun"
            )
        finally:
            process.kill()

    def test_it_returns_promptly(self):
        # The watchdog runs on a timer thread; a slow kill delays the blocked-worker report.
        process = _spawn_tree()
        try:
            started = time.monotonic()
            terminate_tree(process)
            assert time.monotonic() - started < REAP_TIMEOUT_SEC + 10
        finally:
            process.kill()

    def test_an_already_finished_process_is_left_alone(self):
        # Called from `_abort` on a timer, so it can lose the race with normal completion.
        # Signalling a reaped pid is at best pointless and at worst hits a recycled one.
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE)
        process.communicate()
        assert process.poll() is not None

        terminate_tree(process)  # must not raise

    def test_it_does_not_shell_out_for_a_process_that_already_exited(self, monkeypatch):
        called: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kw: called.append(argv)  # type: ignore[arg-type]
        )
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE)
        process.communicate()

        terminate_tree(process)

        assert called == []


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows branch")
class TestWindowsBranch:
    def test_it_asks_taskkill_for_the_whole_tree(self, monkeypatch):
        # /T is the entire point: without it this is `process.kill()` with extra steps.
        recorded: list[list[str]] = []

        def _fake_run(argv, **kwargs):
            recorded.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        process = _spawn_tree()
        try:
            terminate_tree(process)
        finally:
            process.kill()

        assert recorded, "taskkill was never invoked"
        assert recorded[0][:3] == ["taskkill", "/F", "/T"]
        assert recorded[0][-1] == str(process.pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
class TestPosixBranch:
    def test_it_refuses_to_signal_our_own_process_group(self, monkeypatch):
        """
        The guard is load-bearing, not defensive padding.

        `start_new_session=True` is what puts the worker in its own group. If a future edit
        drops that flag, `getpgid(child)` returns *the scheduler's* group and an unguarded
        `killpg` takes down the scheduler along with the worker — every role at once, with
        no log line explaining why.
        """
        from scheduler import adapters

        monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
        killed: list[int] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))

        process = _spawn_tree()
        try:
            adapters._killpg(process)
        finally:
            process.kill()

        assert killed == [], "signalled our own process group"
