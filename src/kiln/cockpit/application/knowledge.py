"""
The `/api/knowledge` payload — pure builders over one gathered snapshot.

Same split as `state.py` and `test_metrics.py`: the HTTP adapter reads the vault config and
the knowledge index and passes plain values across this boundary, so the formatting rules —
which status a payload carries, when "never synced" and "failed" differ — are testable
without a socket, a database or a vault on disk.

The status values are the ones `KnowledgeService.status()` and the index's `overview()`
produce: a project without a configured vault is `not-configured` and is ordinary state, not
an error — most projects have no vault, and a panel that shouts about the absence would
train operators to ignore it.
"""

from __future__ import annotations

from datetime import datetime

from . import state as state_builder

STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"
STATUS_NEVER_SYNCED = "never-synced"
STATUS_NOT_CONFIGURED = "not-configured"
STATUS_UNAVAILABLE = "unavailable"


def unavailable(error: str) -> dict:
    """The payload for an unreadable config: names the fault, spoils only its own panel."""
    return {"status": STATUS_UNAVAILABLE, "configured": False, "error": error}


def build_knowledge(payload: dict, now_local: datetime) -> dict:
    """
    The whole `/api/knowledge` document.

    `payload` is `KnowledgeService.status()`'s output: configuration plus the index's
    `overview()` — the newest sync run, the source row, the document count.
    """
    if not payload.get("configured"):
        return {
            "status": STATUS_NOT_CONFIGURED,
            "configured": False,
            "vault": "",
            "notes": 0,
            "error": 'no vault configured — set "vault" in kiln/project/knowledge.json',
        }
    source = payload.get("source") or {}
    last_run = payload.get("last_run")
    synced_at = last_run.get("finished_at") if last_run else None
    status = STATUS_INDEXED if last_run else STATUS_NEVER_SYNCED
    if source.get("status") == "failed":
        status = STATUS_FAILED
    return {
        "status": status,
        "configured": True,
        "vault": payload.get("vault", ""),
        "exclude": payload.get("exclude", []),
        "notes": payload.get("documents", 0),
        "error": source.get("error"),
        "synced_at": synced_at,
        "synced_ago": _age(synced_at, now_local),
        "last_run": last_run,
    }


def _age(value: str | None, now_local: datetime) -> str | None:
    if not value:
        return None
    try:
        return state_builder.format_age(state_builder.parse_local_timestamp(value), now_local)
    except ValueError:
        return None
