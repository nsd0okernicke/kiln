"""
The KILN-STATUS worker report contract.

A one-shot worker process ends its output with a sentinel line so the scheduler can
decide done-vs-blocked without an LLM judgment step:

    KILN-STATUS: done Added acceptance criteria for order intake

This module owns BOTH the parser and the instruction text given to workers, so the two
cannot drift apart. Generators that inject the instruction (bin/kiln.ps1's
Write-GeneratedWorkerAgent, bin/kiln.sh's write_worker_agent_file) should obtain it via

    python -m scheduler.status_contract --instruction

rather than restating the wording.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

SENTINEL_PREFIX = "KILN-STATUS:"

STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = frozenset({STATUS_DONE, STATUS_BLOCKED})

#: Used when the worker never emitted a sentinel at all — truncated output, a crash, or
#: an agent that ignored the instruction. Treated as `blocked` (never as a scheduler
#: crash) so the retry/escalation path handles it like any other failure.
MISSING_SENTINEL_SUMMARY = "worker produced no KILN-STATUS sentinel; treated as blocked"

WORKER_STATUS_INSTRUCTION = """\
## Required final output line

When you have finished — or determined that you cannot finish — the LAST line of your
output must be exactly one status sentinel, in one of these two forms:

    KILN-STATUS: done <one-line summary of what you accomplished>
    KILN-STATUS: blocked <one-line reason you could not proceed>

Rules:
- Use `done` only when the work is complete and its verification passed.
- Use `blocked` for any outcome you could not complete, for any reason.
- Emit the sentinel exactly once, and keep it on a single line.
- Write nothing after the sentinel line.
"""


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of one worker invocation, as parsed from its stdout."""

    status: str
    summary: str
    #: False when no sentinel line was found. The status is `blocked` either way, but the
    #: scheduler logs and escalation messages distinguish "worker reported blocked" from
    #: "worker never reported", which are different things to debug.
    sentinel_found: bool

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def is_blocked(self) -> bool:
        return self.status == STATUS_BLOCKED


def parse_worker_report(stdout: str) -> WorkerResult:
    """
    Extract the KILN-STATUS sentinel from a worker's stdout.

    Matching rules, deliberately lenient so that a *successful* run is not misread as a
    failure over whitespace or casing:

    - Lines are scanned from the END backwards; the last sentinel line wins. This
      tolerates trailing newlines and banner output, and means a worker that quotes the
      contract earlier in its narrative does not shadow its real verdict.
    - The `KILN-STATUS:` prefix and the status word are both matched case-insensitively,
      and the space after the colon is optional.
    - Anything that is not a recognisable `done` is `blocked`. A missing sentinel, an
      unparseable one, and an explicit `blocked` all converge on the same outcome, since
      the scheduler treats them identically.
    """
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate[: len(SENTINEL_PREFIX)].upper() != SENTINEL_PREFIX:
            continue

        remainder = candidate[len(SENTINEL_PREFIX) :].strip()
        word, _, rest = remainder.partition(" ")
        status = word.strip().lower()
        summary = rest.strip()

        if status in VALID_STATUSES:
            return WorkerResult(status=status, summary=summary, sentinel_found=True)

        detail = f"unrecognised status {word!r}; treated as blocked"
        if summary:
            detail = f"{detail}: {summary}"
        return WorkerResult(status=STATUS_BLOCKED, summary=detail, sentinel_found=True)

    return WorkerResult(
        status=STATUS_BLOCKED,
        summary=MISSING_SENTINEL_SUMMARY,
        sentinel_found=False,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KILN-STATUS worker report contract.")
    parser.add_argument(
        "--instruction",
        action="store_true",
        help="print the canonical worker instruction block for injection into agent files",
    )
    args = parser.parse_args(argv)

    if args.instruction:
        sys.stdout.write(WORKER_STATUS_INSTRUCTION)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
