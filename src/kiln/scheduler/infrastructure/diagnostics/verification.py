"""
The optional per-role verification command — turning a prose quality gate into a real one.

CRAP <= 6, <= 100 mutation sites and the >= 80% Gherkin kill rate live in role files and
`kiln/project/skill-orchestration.md`. In scheduler mode the only thing actually checked
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
import shlex
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
    #: Commit SHA the report was produced from, or empty when unknown.
    commit_sha: str = ""
    #: True when the working tree was clean when the gate ran.
    tree_clean: bool = True

    @property
    def summary(self) -> str:
        """One line for a log or an escalation detail."""
        if self.ok:
            parts = ["verification passed"]
            if self.commit_sha:
                parts.append(f"(commit {self.commit_sha[:12]})")
            if not self.tree_clean:
                parts.append("[dirty tree]")
            return " ".join(parts)
        reason = "timed out" if self.timed_out else "failed"
        first = next((line for line in self.output.splitlines() if line.strip()), "")
        msg = f"verification {reason}: {first}" if first else f"verification {reason}"
        if not self.tree_clean:
            msg += " [dirty tree]"
        return msg


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
    project_root: str | Path | None = None,
) -> VerifyResult:
    """
    Run one verification command in `cwd` and report whether it passed.

    `shell=True` so the profile can write what a person would type -- pipes, `&&`, a venv
    path. That is the same convention `commands.py` already uses for pane commands, and it is
    what makes one string work on both `cmd`/PowerShell and POSIX shells without the profile
    having to know which host it landed on.

    **Stamps every result with the current commit SHA and a clean-tree check.**
    Refuses to run when the working tree is dirty -- a gate result produced from an
    uncommitted tree cannot be trusted (issue #47, finding 3). The caller can override
    this with `allow_dirty=True` for workflows where the gate probes the dirty state.

    Never raises. A command that hangs is killed at the timeout and reported as a failure; a
    command that cannot be started at all is reported the same way. Either one crashing the
    scheduler would take down the role over its own quality gate, which is the opposite of
    what the gate is for.
    """
    command = _expand_paths(command, project_root)
    commit_sha = _read_commit_sha(cwd)
    tree_clean = _check_tree_clean(cwd)
    log.info("running verification: %s  (commit=%s, clean=%s)", command, commit_sha[:12] if commit_sha else "?", tree_clean)
    try:
        completed = _run_command(command, cwd, timeout)
    except subprocess.TimeoutExpired as expired:
        return _timed_out(expired, timeout, commit_sha=commit_sha, tree_clean=tree_clean)
    except OSError as exc:
        log.error("verification could not be started: %s", exc)
        return VerifyResult(ok=False, output=f"could not start {command!r}: {exc}", commit_sha=commit_sha, tree_clean=tree_clean)
    return _completed_result(command, completed, commit_sha=commit_sha, tree_clean=tree_clean)


def _expand_paths(command: str, project_root: str | Path | None) -> str:
    """Expand portable report placeholders without exposing shell-specific syntax to profiles."""
    if project_root is None:
        return command
    root = Path(project_root)
    reports = root / "reports"
    if "{reports}" in command:
        reports.mkdir(parents=True, exist_ok=True)
    quote = subprocess.list2cmdline if os.name == "nt" else lambda values: shlex.quote(values[0])
    return command.replace("{project}", quote([str(root)])).replace(
        "{reports}", quote([str(reports)])
    )


def _run_command(command: str, cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
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


def _check_tree_clean(cwd: str | Path) -> bool:
    """True when `git status --porcelain` in `cwd` produces no output."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0 and not result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        log.debug("could not check tree cleanness for %s", cwd)
        return False


def _read_commit_sha(cwd: str | Path) -> str:
    """The HEAD commit SHA at `cwd`, or empty when unreachable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _timed_out(expired: subprocess.TimeoutExpired, timeout: int, commit_sha: str = "", tree_clean: bool = True) -> VerifyResult:
    output = _decode(expired.stdout) + _decode(expired.stderr)
    log.error("verification timed out after %ss", timeout)
    return VerifyResult(
        ok=False,
        output=tail(output or f"(no output before the {timeout}s timeout)"),
        timed_out=True,
        commit_sha=commit_sha,
        tree_clean=tree_clean,
    )


def _completed_result(command: str, completed: subprocess.CompletedProcess, commit_sha: str = "", tree_clean: bool = True) -> VerifyResult:
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode == 0:
        log.info("verification passed")
        return VerifyResult(ok=True, output=tail(output), commit_sha=commit_sha, tree_clean=tree_clean)
    log.warning("verification failed (exit %s)", completed.returncode)
    return VerifyResult(
        ok=False,
        output=f"`{command}` exited {completed.returncode}\n\n{tail(output)}",
        commit_sha=commit_sha,
        tree_clean=tree_clean,
    )


def run_clean(
    command: str,
    cwd: str | Path,
    timeout: int = DEFAULT_VERIFY_TIMEOUT_SEC,
    project_root: str | Path | None = None,
) -> VerifyResult:
    """
    Like `run`, but refuses to proceed when the tree is dirty.

    A verification result is only trustworthy when the tree matches HEAD:
    otherwise the outcome describes code that was never committed and cannot
    be reproduced from source control.
    """
    result = run(command, cwd, timeout=timeout, project_root=project_root)
    if not result.tree_clean:
        return VerifyResult(
            ok=False,
            output=f"refusing to record gate result: working tree is dirty\n\n{result.output}",
            commit_sha=result.commit_sha,
            tree_clean=False,
        )
    return result


def _decode(value: bytes | str | None) -> str:
    """TimeoutExpired's captured streams are bytes even in text mode."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
