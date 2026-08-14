"""
The optional per-role verification command — turning a prose quality gate into a real one.

CRAP <= 6, <= 100 mutation sites and the >= 80% Gherkin kill rate live in role files and
`constitution/skill-orchestration.md`. In scheduler mode the only thing actually checked
before a handoff is that the worker's last line matched `KILN-STATUS: done`
(`status_contract.py`). Nothing ran tests, coverage, mutation or lint. A worker that skipped
every gate and claimed `done` was believed — in precisely the mode designed to run unattended.

A role may therefore declare `"verify": "pytest -q"`. The scheduler runs it in that role's own
worktree after the worker reports done, and treats a non-zero exit as a failed attempt.

Deliberately opaque: the command is a string and the only thing inspected is its exit code, so
nothing language- or toolchain-specific enters the framework.

**Trust.** `verify` is arbitrary code from the profile, running with the scheduler's own
privileges. That is the same trust level the profile already carries — it chooses which
binaries run as agents, with which models and permissions — so it is acceptable, but it is
stated here rather than left implicit.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Default ceiling for one verification run. Deliberately *not* the worker timeout (900s,
#: sized for a whole LLM session): a test suite is a different beast, and a hung verify must
#: not quietly consume the budget meant for the work itself.
DEFAULT_VERIFY_TIMEOUT_SEC = 300

#: Lines of output carried into the retry prompt. A failing suite can emit megabytes, and
#: pasting that into the next prompt is both expensive and useless.
MAX_OUTPUT_LINES = 40

#: Characters, after the line cap -- one pathological line can still be enormous.
MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one verification run."""

    ok: bool
    output: str
    #: True when the command was killed at the timeout rather than finishing on its own.
    timed_out: bool = False

    @property
    def summary(self) -> str:
        """One line for a log or an escalation detail."""
        if self.ok:
            return "verification passed"
        reason = "timed out" if self.timed_out else "failed"
        first = next((line for line in self.output.splitlines() if line.strip()), "")
        return f"verification {reason}: {first}" if first else f"verification {reason}"


def tail(output: str, max_lines: int = MAX_OUTPUT_LINES, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """
    The last `max_lines` of output, then hard-capped by characters.

    The *tail*, not the head: every test runner worth the name puts its summary at the bottom,
    and the first forty lines of a failing suite are usually collection noise.
    """
    lines = output.splitlines()
    clipped = lines[-max_lines:]
    text = "\n".join(clipped)
    if len(lines) > max_lines:
        text = f"[... {len(lines) - max_lines} earlier line(s) omitted]\n{text}"
    if len(text) > max_chars:
        text = "[... truncated]\n" + text[-max_chars:]
    return text


def run(
    command: str,
    cwd: str | Path,
    timeout: int = DEFAULT_VERIFY_TIMEOUT_SEC,
) -> VerifyResult:
    """
    Run one verification command in `cwd` and report whether it passed.

    `shell=True` so the profile can write what a person would type -- pipes, `&&`, a venv
    path. That is the same convention `commands.py` already uses for pane commands, and it is
    what makes one string work on both `cmd`/PowerShell and POSIX shells without the profile
    having to know which host it landed on.

    Never raises. A command that hangs is killed at the timeout and reported as a failure; a
    command that cannot be started at all is reported the same way. Either one crashing the
    scheduler would take down the role over its own quality gate, which is the opposite of
    what the gate is for.
    """
    log.info("running verification: %s", command)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            # A worker may have left these pointing at the capture proxy; verification is
            # not an agent call and has no business inheriting an LLM base URL.
            env={k: v for k, v in os.environ.items() if not k.endswith("_BASE_URL")},
        )
    except subprocess.TimeoutExpired as expired:
        output = _decode(expired.stdout) + _decode(expired.stderr)
        log.error("verification timed out after %ss", timeout)
        return VerifyResult(
            ok=False,
            output=tail(output or f"(no output before the {timeout}s timeout)"),
            timed_out=True,
        )
    except OSError as exc:
        log.error("verification could not be started: %s", exc)
        return VerifyResult(ok=False, output=f"could not start {command!r}: {exc}")

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode == 0:
        log.info("verification passed")
        return VerifyResult(ok=True, output=tail(output))
    log.warning("verification failed (exit %s)", completed.returncode)
    return VerifyResult(
        ok=False,
        output=f"`{command}` exited {completed.returncode}\n\n{tail(output)}",
    )


def _decode(value: bytes | str | None) -> str:
    """TimeoutExpired's captured streams are bytes even in text mode."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
