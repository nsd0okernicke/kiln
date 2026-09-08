"""
Standalone auto-approver for unattended test runs.

Run this in a separate terminal alongside a swarm that was launched without
`--auto-approve-spec`. It forwards the specifier's scenarios to the coder as though a human
had clicked approve.

The forwarding itself lives in `kiln.scheduler.infrastructure.persistence.spec_approval`,
which is also what the human-in-the-loop scheduler and the cockpit start behind their own
flags. This file is only the terminal in front of it: three copies of the same SQL is how
they drifted apart the first time.

Usage:
    python tools/auto_approve_spec.py <project-root>
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from kiln.scheduler.infrastructure.persistence import spec_approval


def _watch(db_path: Path) -> None:
    while True:
        try:
            for work_item in spec_approval.forward_pending_specs(db_path):
                print(f"[{datetime.now(UTC):%H:%M:%S}] Auto-approved {work_item} -> coder")
        except Exception as exc:  # a locked database is expected while the swarm writes
            print(f"Error: {exc}", file=sys.stderr)
        time.sleep(spec_approval.POLL_SECONDS)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python auto_approve_spec.py <project-root>")
        return 1

    db_path = Path(sys.argv[1]).resolve() / ".kiln" / "messages.db"
    if not db_path.is_file():
        print(f"Database not found: {db_path}")
        return 1

    print(f"Watching {db_path}")
    print("Press Ctrl+C to stop.\n")
    try:
        _watch(db_path)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
