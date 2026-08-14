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
import re
import sys
from dataclasses import dataclass

SENTINEL_PREFIX = "KILN-STATUS:"

#: Optional second sentinel: the stable name for the piece of work this cycle is about.
#:
#: It exists because in scheduler mode the *scheduler* composes the outbound handoff, copying
#: `Handoff:` from the inbound message verbatim -- so a worker had no channel to name anything,
#: and `roles/specifier.md`'s instruction to "invent the handoff name, replacing the `pending`
#: placeholder" was unimplementable. The placeholder propagated through every hop of every
#: cycle, and every message in the queue ended up in one `work_item` bucket called `pending`.
#:
#: That is not cosmetic: `count_work_item_arrivals` backs the max-cycles guard and
#: `spend_by_work_item` backs the cost cap, so one shared bucket makes both of them count
#: across unrelated features.
HANDOFF_PREFIX = "KILN-HANDOFF:"

STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = frozenset({STATUS_DONE, STATUS_BLOCKED})

#: The placeholder a human puts in their opening request. The specifier replaces it; every
#: later role carries the real name through unchanged. Kept here rather than imported from
#: `send.py` because both the parser and the scheduler need it and neither should depend on
#: the CLI module.
PENDING_HANDOFF = "pending"

#: Characters allowed in a work-item name. A name becomes a database grouping key and appears
#: in log lines and commit subjects, so a worker that answers with a sentence -- or with
#: something containing a quote -- must not become the key everything is grouped by.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}$")

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

## Naming the work (only when the inbound handoff says `pending`)

If — and only if — the inbound handoff's `Handoff:` field is the placeholder `pending`, you
are the role that names this piece of work. Emit one extra line immediately BEFORE the status
sentinel:

    KILN-HANDOFF: <short stable name for this work>

The name is what ties every later message, cost figure and cycle count back to this one piece
of work, so choose it once and choose it well:

- Short and descriptive, like a branch name: `cat-3-search-by-author`, `fix-isbn-validation`.
- Letters, digits, spaces, `-`, `_`, `.` and `/` only; 80 characters at most.
- Never the word `pending` — that is the placeholder you are replacing.

If the inbound `Handoff:` already names the work, **do not emit this line**. Carrying the
existing name through unchanged is what makes the grouping mean anything.
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
    #: The name this worker gave the piece of work, or '' when it named none. Only ever
    #: honoured for the hop that names a work item -- see `parse_handoff_name`.
    handoff_name: str = ""

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
            return WorkerResult(
                status=status,
                summary=summary,
                sentinel_found=True,
                # Only a completed cycle can name work: a blocked one produced none, and its
                # escalation carries the inbound name so the human can find what failed.
                handoff_name=parse_handoff_name(stdout) if status == STATUS_DONE else "",
            )

        detail = f"unrecognised status {word!r}; treated as blocked"
        if summary:
            detail = f"{detail}: {summary}"
        return WorkerResult(status=STATUS_BLOCKED, summary=detail, sentinel_found=True)

    return WorkerResult(
        status=STATUS_BLOCKED,
        summary=MISSING_SENTINEL_SUMMARY,
        sentinel_found=False,
    )


def parse_handoff_name(stdout: str) -> str:
    """
    Extract the optional `KILN-HANDOFF:` sentinel, or '' when there is none.

    Scanned from the end like the status sentinel, and validated rather than trusted: the
    value becomes a database grouping key, so a worker that answers with a paragraph, an
    empty string, or the `pending` placeholder it was supposed to replace contributes
    nothing instead of poisoning the key everything is grouped by.

    Rejecting silently is deliberate. The caller falls back to the inbound name, which is
    the behaviour that existed before this sentinel — a malformed name must not fail a cycle
    whose actual work succeeded.
    """
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate[: len(HANDOFF_PREFIX)].upper() != HANDOFF_PREFIX:
            continue
        name = candidate[len(HANDOFF_PREFIX) :].strip().strip("\"'")
        if name.lower() == PENDING_HANDOFF or not _NAME_RE.match(name):
            return ""
        return name
    return ""


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
