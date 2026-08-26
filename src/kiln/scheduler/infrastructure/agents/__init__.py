"""
Per-backend one-shot worker invocation.

Each adapter splits into a pure `build_command(...)` (argv construction, unit-testable
without spawning anything) and a thin `run_worker(...)` that shells out. That boundary is
where constitution/engineering.md's "environmentally unsuitable" line falls: everything
above it is tested directly, and only the subprocess call itself needs a live backend.

`WorkerInvocation` lives here rather than inside any one adapter module: every backend adapter
returns it, so it belongs to the package, not whichever adapter happened to be written first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ...domain.models import TokenUsage, WorkerInvocation

log = logging.getLogger("kiln-scheduler")

#: How long to wait for a killed tree to actually go away before giving up on it. The reader
#: loop is already unblocked by then -- this only bounds the tidy-up.
REAP_TIMEOUT_SEC = 5.0

#: Silence that means "stopped", not "thinking". Every hang seen live went quiet and
#: never spoke again; healthy workers emit events continuously.
DEFAULT_IDLE_TIMEOUT_SEC = 300


class Watchdog:
    """
    Ends a worker that has either run too long or stopped producing output.

    Two limits, because they catch different failures and only one of them was here before.
    `timeout` bounds a worker that is genuinely working and merely slow. `idle_timeout` bounds
    one that has *stopped* -- and stopping is what every hang observed live actually did:

    - a `testcontainers` fixture waiting on a Docker daemon that was not running
    - a PowerShell activation script that never returned
    - a codex code-mode cell reporting "Wall time 11.0 seconds" unchanged, forever

    None of the three ever recovered, and none produced a single line of output after going
    quiet. With only a total cap, the last of them cost 60 minutes for 16 minutes of work:
    silence began at 08:11 and the cap fired at 08:56. The idle limit turns that hour into
    minutes, and it cannot fire on healthy work, because a working agent emits events
    continuously -- which is the same property the pane already relies on to stay live.

    Reason is None while the worker is behaving; after `stop()` it holds the human-readable
    cause, so the caller reports *why* it was killed rather than a generic timeout.
    """

    #: How often the idle check wakes. Fine-grained enough that the reported silence is
    #: roughly true, coarse enough to be free.
    POLL_SEC = 5.0

    def __init__(self, process: subprocess.Popen, timeout: float, idle_timeout: float | None):
        self._process = process
        self._timeout = timeout
        self._idle_timeout = idle_timeout
        self._last_output = time.monotonic()
        self._started = self._last_output
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.reason: str | None = None

    def saw_output(self) -> None:
        """Call for every line read. Cheap by design -- it runs per line of worker output."""
        with self._lock:
            self._last_output = time.monotonic()

    def start(self) -> Watchdog:
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()

    def _watch(self) -> None:
        while not self._done.wait(self.POLL_SEC):
            now = time.monotonic()
            if now - self._started >= self._timeout:
                self._fire(f"worker timed out after {self._timeout:.0f}s")
                return
            with self._lock:
                quiet = now - self._last_output
            if self._idle_timeout is not None and quiet >= self._idle_timeout:
                self._fire(
                    f"worker produced no output for {quiet:.0f}s "
                    f"(idle limit {self._idle_timeout:.0f}s)"
                )
                return

    def _fire(self, reason: str) -> None:
        self.reason = reason
        terminate_tree(self._process)


@dataclass(frozen=True)
class StreamCapture:
    """A worker's raw JSON-lines stream plus any watchdog termination reason."""

    stdout: str
    timeout_reason: str | None


def capture_json_stream(
    process: subprocess.Popen,
    *,
    timeout: float,
    idle_timeout: float | None,
    render_event: Callable[[dict], Iterable[str]],
    emit: Callable[[str], None],
    watchdog_factory=Watchdog,
    terminate: Callable[[subprocess.Popen], None] | None = None,
) -> StreamCapture:
    """Consume one adapter's JSON-lines stream with shared watchdog/reaping semantics."""
    watchdog = watchdog_factory(process, timeout, idle_timeout).start()
    captured: list[str] = []
    try:
        for line in process.stdout:  # type: ignore[union-attr]
            watchdog.saw_output()
            captured.append(line)
            _render_json_line(line, render_event, emit)
        try:
            process.wait(timeout=REAP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            (terminate or terminate_tree)(process)
    finally:
        watchdog.stop()
    return StreamCapture("".join(captured), watchdog.reason)


def _render_json_line(
    line: str, render_event: Callable[[dict], Iterable[str]], emit: Callable[[str], None]
) -> None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return
    for rendered in render_event(event):
        emit(rendered)


def terminate_tree(process: subprocess.Popen) -> None:
    """Best-effort termination of a worker and its descendants."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        # /T covers the descendant chain; /F because a hung child will not honour a request.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        _killpg(process)

    # Belt and braces: taskkill can miss a process started under a different account, and
    # `_killpg` declines outright when the group looks like our own.
    with contextlib.suppress(OSError):
        process.kill()
    try:
        process.wait(timeout=REAP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        log.warning("worker pid %s outlived its kill; leaving it to the OS", process.pid)


def _killpg(process: subprocess.Popen) -> None:
    """Signal the child's process group without ever signalling our own."""
    try:
        group = os.getpgid(process.pid)
    except OSError:
        # Gone, not ours, or not a pid at all — all of which mean there is nothing to signal.
        return
    if group == os.getpgrp():
        log.warning("worker pid %s shares our process group; not signalling it", process.pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SEC",
    "REAP_TIMEOUT_SEC",
    "StreamCapture",
    "TokenUsage",
    "Watchdog",
    "WorkerInvocation",
    "capture_json_stream",
    "terminate_tree",
]
