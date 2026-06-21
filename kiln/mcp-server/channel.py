"""
channel.py  —  SQLite-backed push channel for cross-process agent communication.

How it works
------------
- Agent calls ch.send()  → writes a row to SQLite
- The OS (inotify / FSEvents / kqueue) detects the file change instantly
- Agent's watchdog handler fires → reads the new row → calls your callback
- No polling. Zero extra infrastructure beyond `pip install watchdog`.

Usage
-----
# Subscribing agent — subscribe first, then keep process alive
ch = Channel("messages.db")
ch.subscribe("AgentName", lambda msg: handle(msg))
done.wait()  # blocks until done

# Sending agent — just send
ch = Channel("messages.db")
ch.send(Message(sender="Agent1", receiver="Agent2", topic="work.done", payload={...}))
"""

import json
import os
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

@dataclass
class Message:
    sender: str
    receiver: str          # agent name, or "*" for broadcast to all subscribers
    topic: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

class Channel:
    """
    Push channel backed by SQLite + OS file-system events (via watchdog).

    - send()       : called by the producing agent — persists the message
    - subscribe()  : called by the consuming agent — registers a callback
                     that fires the instant a new message arrives
    - ack(id)      : mark message as successfully processed
    - nack(id)     : mark message as failed (kept in DB for inspection)
    - history()    : inspect all messages in the log
    - stop()       : clean up the file-system watcher
    """

    def __init__(self, db_path: str = "messages.db"):
        self.db_path = os.path.abspath(db_path)
        # In WAL mode the write that other processes see lands in the -wal file first
        self._wal_path = self.db_path + "-wal"
        self._callbacks: list[tuple[str, Callable]] = []
        self._seen: set[str] = set()          # ids already dispatched this session
        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        TEXT PRIMARY KEY,
                    sender    TEXT NOT NULL,
                    receiver  TEXT NOT NULL,
                    topic     TEXT NOT NULL,
                    payload   TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status    TEXT NOT NULL DEFAULT 'pending',
                    branch    TEXT DEFAULT 'main'
                    -- status: pending | done | failed
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_receiver_status "
                "ON messages(receiver, status)"
            )
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Send  (producing agent)
    # ------------------------------------------------------------------

    def send(self, msg: Message) -> Message:
        """Persist a message. Triggers any subscribed agents instantly via OS event."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (id, sender, receiver, topic, payload, timestamp, status, branch) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (msg.id, msg.sender, msg.receiver, msg.topic,
                 json.dumps(msg.payload), msg.timestamp, getattr(msg, 'branch', 'main')),
            )
            conn.commit()
        print(f"[Channel] SENT  id={msg.id[:8]}  topic={msg.topic}  -> {msg.receiver}")
        return msg

    # ------------------------------------------------------------------
    # Subscribe  (consuming agent)
    # ------------------------------------------------------------------

    def subscribe(self, receiver: str, callback: Callable[[Message], None]):
        """
        Register a push callback for this receiver.
        `callback` is called in a background thread when a message arrives.
        Call this once at startup; keep the process alive (e.g. Event.wait()).
        """
        self._callbacks.append((receiver, callback))
        if not self._observer:
            self._start_watcher()

    def _start_watcher(self):
        db_dir = os.path.dirname(self.db_path) or "."
        db_name = os.path.basename(self.db_path)
        wal_name = os.path.basename(self._wal_path)
        channel = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):
                # Fire on either the -wal file (mid-transaction) or the .db file (checkpoint)
                name = os.path.basename(event.src_path)
                if name in (wal_name, db_name):
                    channel._dispatch()

        self._observer = Observer()
        self._observer.schedule(_Handler(), db_dir, recursive=False)
        self._observer.daemon = False  # Important: non-daemon so process stays alive
        self._observer.start()

    def _dispatch(self):
        """Read pending rows and push each to matching subscribers (once per id)."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE status = 'pending' ORDER BY timestamp"
                ).fetchall()

            for row in rows:
                if row["id"] in self._seen:
                    continue
                for receiver, cb in self._callbacks:
                    if row["receiver"] in (receiver, "*"):
                        self._seen.add(row["id"])
                        msg = Message(
                            id=row["id"],
                            sender=row["sender"],
                            receiver=row["receiver"],
                            topic=row["topic"],
                            payload=json.loads(row["payload"]),
                            timestamp=row["timestamp"],
                        )
                        # Run callback in its own thread so a slow handler
                        # doesn't block the watcher or other callbacks
                        threading.Thread(
                            target=self._safe_call, args=(cb, msg), daemon=True
                        ).start()

    def _safe_call(self, cb: Callable, msg: Message):
        try:
            cb(msg)
        except Exception as exc:
            print(f"[Channel] WARNING callback error for id={msg.id[:8]}: {exc}")

    # ------------------------------------------------------------------
    # Acknowledge / fail
    # ------------------------------------------------------------------

    def ack(self, msg_id: str):
        """Mark message as successfully processed."""
        self._set_status(msg_id, "done")

    def nack(self, msg_id: str):
        """Mark message as failed (kept in DB for inspection / manual retry)."""
        self._set_status(msg_id, "failed")

    def _set_status(self, msg_id: str, status: str):
        with self._conn() as conn:
            conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, msg_id))
            conn.commit()

    # ------------------------------------------------------------------
    # Inspect log
    # ------------------------------------------------------------------

    def history(self, limit: int = 50) -> list[dict]:
        """Return recent messages from the log (newest first)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self):
        """Stop the file-system watcher. Call on process shutdown."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
