"""Deterministic executable impersonating Claude Code for system tests."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _status() -> str:
    sequence_path = os.environ.get("KILN_FAKE_SEQUENCE_FILE")
    if not sequence_path:
        return os.environ.get("KILN_FAKE_STATUS", "done")
    path = Path(sequence_path)
    statuses = path.read_text(encoding="utf-8").splitlines()
    status = statuses.pop(0) if statuses else "done"
    path.write_text("\n".join(statuses), encoding="utf-8")
    return status


def main() -> int:
    status = _status()
    summary = os.environ.get("KILN_FAKE_SUMMARY", "completed deterministic worker task")
    handoff = os.environ.get("KILN_FAKE_HANDOFF", "system-test-task")

    if status == "done":
        changed = Path(os.environ.get("KILN_FAKE_FILE", "system-worker.txt"))
        changed.write_text(
            os.environ.get("KILN_FAKE_CONTENT", "written by deterministic worker\n"),
            encoding="utf-8",
        )

    lines = []
    if status == "done" and handoff:
        lines.append(f"KILN-HANDOFF: {handoff}")
    lines.append(f"KILN-STATUS: {status} {summary}")
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "\n".join(lines),
                "total_cost_usd": 0.0,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
