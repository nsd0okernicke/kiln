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
from kiln.scheduler.infrastructure.agents import REAP_TIMEOUT_SEC, Watchdog, terminate_tree

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


class TestWatchdog:
    """
    The total cap alone bills the full timeout for a worker that already stopped.

    Measured on the run that prompted this: the coder went silent at 08:11:56 and the 3600s
    cap fired at 08:56:32. Forty-four of those sixty minutes were spent waiting on a process
    that had produced its last byte three quarters of an hour earlier. Every hang seen live
    behaved the same way -- silent, and never speaking again.
    """

    def _quiet_process(self) -> subprocess.Popen:
        """Prints once, then goes quiet without exiting -- the shape of every live hang."""
        code = f"print('hello', flush=True); import time; time.sleep({SLEEP_SEC})"
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )

    def test_it_kills_a_worker_that_has_gone_quiet(self):
        process = self._quiet_process()
        try:
            watchdog = Watchdog(process, timeout=600, idle_timeout=2)
            watchdog.POLL_SEC = 0.2
            watchdog.start()
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "hello"
            watchdog.saw_output()

            assert process.stdout.read() == "", "the read never unblocked"

            assert watchdog.reason is not None
            assert "no output" in watchdog.reason
            watchdog.stop()
        finally:
            process.kill()

    def test_it_reports_silence_not_a_generic_timeout(self):
        # The distinction is the whole diagnostic value: "slow" and "stopped" want different
        # responses from whoever reads the escalation.
        process = self._quiet_process()
        try:
            watchdog = Watchdog(process, timeout=600, idle_timeout=1)
            watchdog.POLL_SEC = 0.2
            watchdog.start()
            assert process.stdout is not None
            process.stdout.read()
            watchdog.stop()

            assert "timed out after" not in (watchdog.reason or "")
            assert "idle limit" in (watchdog.reason or "")
        finally:
            process.kill()

    def test_output_keeps_a_busy_worker_alive(self):
        # It must not fire on healthy work. A worker emitting events resets the clock, so a
        # long-but-productive cycle runs to its real cap.
        code = (
            "import time\n"
            "for _ in range(20):\n"
            "    print('tick', flush=True)\n"
            "    time.sleep(0.1)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            watchdog = Watchdog(process, timeout=600, idle_timeout=1)
            watchdog.POLL_SEC = 0.1
            watchdog.start()
            assert process.stdout is not None
            lines = 0
            for _line in process.stdout:
                watchdog.saw_output()
                lines += 1
            watchdog.stop()

            assert lines == 20, f"worker was cut short after {lines} lines"
            assert watchdog.reason is None
        finally:
            process.kill()

    def test_the_total_cap_still_applies_to_a_chatty_worker(self):
        # A worker can be productive and still overrun; idle must not replace the total cap.
        code = "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.05)\n"
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            watchdog = Watchdog(process, timeout=1, idle_timeout=600)
            watchdog.POLL_SEC = 0.1
            watchdog.start()
            assert process.stdout is not None
            for _line in process.stdout:
                watchdog.saw_output()
            watchdog.stop()

            assert "timed out after" in (watchdog.reason or "")
        finally:
            process.kill()

    @pytest.mark.parametrize("disabled", [None, 0], ids=["none", "zero"])
    def test_it_can_be_switched_off(self, disabled):
        # `--worker-idle-timeout 0` for a backend that legitimately works in long silences.
        # 0 must mean *off*, not "kill on the first poll" -- the scheduler maps it to None,
        # and a watchdog that read it literally would kill every worker before its first line.
        disabled = disabled or None  # exactly what role_scheduler does with the CLI value
        process = self._quiet_process()
        try:
            watchdog = Watchdog(process, timeout=600, idle_timeout=disabled)
            watchdog.POLL_SEC = 0.1
            watchdog.start()
            time.sleep(1)
            watchdog.stop()

            assert watchdog.reason is None
            assert process.poll() is None, "the worker was killed with idle checking disabled"
        finally:
            process.kill()


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
        from kiln.scheduler.infrastructure import agents as adapters

        monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
        killed: list[int] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))

        process = _spawn_tree()
        try:
            adapters._killpg(process)
        finally:
            process.kill()

        assert killed == [], "signalled our own process group"
