"""Deterministic executable impersonating Pi's JSON event mode."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    sys.stdin.read()
    changed = Path(os.environ.get("KILN_FAKE_FILE", "system-worker.txt"))
    changed.write_text("written by deterministic Pi worker\n", encoding="utf-8")
    handoff = os.environ.get("KILN_FAKE_HANDOFF", "system-test-task")
    report = f"KILN-HANDOFF: {handoff}\nKILN-STATUS: done completed deterministic Pi worker task"
    print(json.dumps({"type": "session", "version": 3, "id": "fake-pi"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": report}],
                    "usage": {"input": 3, "output": 2, "cacheRead": 1, "cacheWrite": 0},
                },
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_settled"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
