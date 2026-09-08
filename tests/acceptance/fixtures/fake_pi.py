"""Deterministic executable impersonating Pi's JSON event mode.

Honours the same `KILN_FAKE_*` contract as `fake_claude.py`, so a scenario can drive either
backend without changing how it sets up. The two differ only in wire format: Pi emits a
sequence of JSON events and reads its prompt from stdin, Claude emits one `result` object and
takes the prompt in argv.

That argv difference is why Pi is the default backend for these scenarios. A realistic worker
definition is ~33KB and Claude's adapter passes it inline as `--agents`, which exceeds the
32,767-character `CreateProcess` limit on Windows and fails with `WinError 206` before the
worker starts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _status() -> str:
    """The status for this invocation, consuming one line of a sequence file when given.

    A sequence lets one scenario drive several cycles with different outcomes -- blocked, then
    done -- without re-launching anything in between.
    """
    sequence_path = os.environ.get("KILN_FAKE_SEQUENCE_FILE")
    if not sequence_path:
        return os.environ.get("KILN_FAKE_STATUS", "done")
    path = Path(sequence_path)
    statuses = path.read_text(encoding="utf-8").splitlines()
    status = statuses.pop(0) if statuses else "done"
    path.write_text("\n".join(statuses), encoding="utf-8")
    return status


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main() -> int:
    sys.stdin.read()
    status = _status()
    summary = os.environ.get("KILN_FAKE_SUMMARY", "completed deterministic Pi worker task")
    handoff = os.environ.get("KILN_FAKE_HANDOFF", "system-test-task")

    if status == "done":
        changed = Path(os.environ.get("KILN_FAKE_FILE", "system-worker.txt"))
        changed.write_text(
            os.environ.get("KILN_FAKE_CONTENT", "written by deterministic Pi worker\n"),
            encoding="utf-8",
        )

    lines = []
    if status == "done" and handoff:
        lines.append(f"KILN-HANDOFF: {handoff}")
    lines.append(f"KILN-STATUS: {status} {summary}")
    report = "\n".join(lines)

    _emit({"type": "session", "version": 3, "id": "fake-pi"})
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": report}],
                "usage": {"input": 3, "output": 2, "cacheRead": 1, "cacheWrite": 0},
            },
        }
    )
    _emit({"type": "agent_settled"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
