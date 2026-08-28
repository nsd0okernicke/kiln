"""
Message-queue data access for .kiln/messages.db.

Single source of truth for every SQL statement touching the queue. The MCP channel
server (src/kiln/mcp_server/channel.py) and the scheduler both call in here, so the
queue's semantics are defined once instead of being restated in prose in
kiln/project/skills/kiln-handoff/SKILL.md and re-implemented per caller.

Every function takes `db_path` explicitly — there is no module-level connection — so
tests point these at a scratch file with the real schema instead of mocking SQLite.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from ...domain.models import DEFAULT_PRIORITY as DEFAULT_PRIORITY
from ...domain.models import MessageStatus
from .queue_commands import (
    _set_status as _set_status,
)
from .queue_commands import (
    acknowledge_message as acknowledge_message,
)
from .queue_commands import (
    failed_messages as failed_messages,
)
from .queue_commands import (
    fetch_and_deliver as fetch_and_deliver,
)
from .queue_commands import (
    fetch_resume as fetch_resume,
)
from .queue_commands import (
    get_message as get_message,
)
from .queue_commands import (
    insert_handoff as insert_handoff,
)
from .queue_commands import (
    mark_failed as mark_failed,
)
from .queue_commands import (
    mark_processed as mark_processed,
)
from .queue_commands import (
    mark_processing as mark_processing,
)
from .queue_commands import (
    message_exists as message_exists,
)
from .queue_commands import (
    name_work_item as name_work_item,
)
from .queue_commands import (
    recover_stale_processing as recover_stale_processing,
)
from .queue_commands import (
    resume_failed as resume_failed,
)
from .queue_queries import (
    WORK_ITEM_WINDOW as WORK_ITEM_WINDOW,
)
from .queue_queries import (
    count_queued as count_queued,
)
from .queue_queries import (
    count_queued_by_role as count_queued_by_role,
)
from .queue_queries import (
    count_work_item_arrivals as count_work_item_arrivals,
)
from .queue_queries import (
    cycles_by_work_item as cycles_by_work_item,
)
from .queue_queries import (
    oldest_queued_by_role as oldest_queued_by_role,
)
from .queue_queries import (
    pending_for_role as pending_for_role,
)
from .queue_queries import (
    recent_messages as recent_messages,
)
from .queue_queries import (
    work_item_messages as work_item_messages,
)
from .queue_storage import connect as connect
from .task_store import (
    TASK_ACTIVE as TASK_ACTIVE,
)
from .task_store import (
    TASK_ARCHIVED as TASK_ARCHIVED,
)
from .task_store import (
    TASK_BACKLOG as TASK_BACKLOG,
)

STATUS_QUEUED = MessageStatus.QUEUED.value
STATUS_DELIVERED = MessageStatus.DELIVERED.value
STATUS_PROCESSING = MessageStatus.PROCESSING.value
STATUS_PROCESSED = MessageStatus.PROCESSED.value
STATUS_FAILED = MessageStatus.FAILED.value

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  sender TEXT NOT NULL,
  target TEXT NOT NULL,
  priority INTEGER DEFAULT 50,
  status TEXT DEFAULT 'queued',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  delivered_at TEXT,
  acked_at TEXT,
  processed_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  work_item TEXT
)
"""

INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status)"

#: Groups every message belonging to one piece of work, newest last.
#:
#: `branch` cannot do this job: it holds the *base* branch, which every role on a swarm
#: shares, so grouping by it groups everything into one bucket. Without a real grouping key
#: nothing can answer "what did this feature cost" or "how many cycles has it been round",
#: and loop detection has nothing to count.
WORK_ITEM_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_work_item ON messages(work_item,created_at)"

TASK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  branch TEXT NOT NULL,
  work_item TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog' CHECK (status IN ('backlog', 'active', 'archived')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  dispatched_at TEXT,
  message_id TEXT REFERENCES messages(id),
  UNIQUE (branch, work_item)
)
"""
TASK_STATUS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_branch_status ON tasks(branch,status,created_at)"
)
TASK_CONTEXT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_context (
  branch TEXT PRIMARY KEY,
  human_role TEXT NOT NULL,
  intake_role TEXT NOT NULL,
  sequential INTEGER NOT NULL DEFAULT 0
)
"""


def ensure_schema(db_path: str | Path) -> None:
    """
    Create the messages table and index if absent, and enable WAL.

    Mirrors the CREATE half of bin/kiln.ps1's inline init script (bin/kiln.ps1:1690).
    NOTE: the one-time repair for legacy tables whose `created_at` was NOT NULL without a
    default is intentionally NOT ported here — kiln.ps1 still owns that migration. Port it
    before this function replaces that inline script, or the repair is silently lost.

    **This creates; it does not migrate.** `CREATE TABLE IF NOT EXISTS` leaves an existing
    table exactly as it is, so a database created before `work_item` existed keeps the old
    shape and every insert naming that column fails with "no such column". That is accepted
    deliberately while Kiln is used only for test projects: delete `.kiln/messages.db` and
    let it be recreated. Add an ordered-migrations step here before anyone runs a project
    they cannot throw away.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.execute(SCHEMA_SQL)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        for column in ("started_at", "finished_at"):
            if column not in columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
        conn.execute(INDEX_SQL)
        conn.execute(WORK_ITEM_INDEX_SQL)
        conn.execute(TASK_SCHEMA_SQL)
        conn.execute(TASK_STATUS_INDEX_SQL)
        conn.execute(TASK_CONTEXT_SCHEMA_SQL)
        ctx_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_context)")}
        if "sequential" not in ctx_columns:
            conn.execute(
                "ALTER TABLE task_context ADD COLUMN sequential "
                "INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
