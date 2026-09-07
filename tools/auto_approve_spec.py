"""
Standalone auto-approver for unattended test runs.

Run this in a separate terminal alongside the swarm. It polls the message
database every 5 seconds and auto-forwards specifier→human messages to coder.

Usage:
    python tools/auto_approve_spec.py <project-root>

Example:
    python tools/auto_approve_spec.py C:\projekte\agentic-coding\library-hub-testrun7
"""

import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _approve_pending(db_path: Path) -> None:
    """Find specifier→human messages and forward them to coder."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, work_item FROM messages "
            "WHERE sender='specifier' AND target='human-in-the-loop' "
            "AND acked_at IS NULL "
            "AND (status = 'processed' OR status = 'queued' OR status = 'delivered') "
            "ORDER BY created_at ASC"
        )
        for msg_id, work_item in cur.fetchall():
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            wi = work_item or "unknown"
            approval = (
                f"Sender: human-in-the-loop\n"
                f"Handoff: {wi}\n"
                f"Branch: main\n"
                f"Commit: \n\n"
                f"Auto-approved (standalone auto-approver).\n"
                f"Next role: coder\n"
            )
            cur.execute(
                "INSERT INTO messages (sender, target, priority, status, content, "
                "created_at, work_item, branch) "
                "VALUES (?, ?, 50, 'queued', ?, ?, ?, 'main')",
                ("human-in-the-loop", "coder", approval, now, wi),
            )
            cur.execute(
                "UPDATE messages SET acked_at=? WHERE id=? AND acked_at IS NULL",
                (now, msg_id),
            )
            conn.commit()
            print(f"[{now[11:19]}] Auto-approved {wi} (msg {msg_id[:8]}) → coder")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python auto_approve_spec.py <project-root>")
        sys.exit(1)

    db_path = Path(sys.argv[1]).resolve() / ".kiln" / "messages.db"
    if not db_path.is_file():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    print(f"Watching {db_path}")
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            _approve_pending(db_path)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
        time.sleep(5.0)


if __name__ == "__main__":
    main()
