"""Typed queue contracts shared by the scheduler and its driving adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

DEFAULT_PRIORITY = 50


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class QueueMessage(TypedDict, total=False):
    id: str
    sender: str
    target: str
    priority: int
    status: str
    content: str
    created_at: str
    delivered_at: str | None
    acked_at: str | None
    processed_at: str | None
    error: str | None
    branch: str
    work_item: str | None


@dataclass(frozen=True)
class WorkerRequest:
    """One application-level request to execute a role worker."""

    prompt: str
    attempt: int = 1
    max_budget_usd: float | None = None


ALLOWED_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.QUEUED: frozenset(
        {
            MessageStatus.DELIVERED,
            MessageStatus.PROCESSING,
            MessageStatus.PROCESSED,
            MessageStatus.FAILED,
        }
    ),
    MessageStatus.DELIVERED: frozenset(
        {
            MessageStatus.DELIVERED,
            MessageStatus.PROCESSING,
            MessageStatus.PROCESSED,
            MessageStatus.FAILED,
        }
    ),
    MessageStatus.PROCESSING: frozenset(
        {MessageStatus.DELIVERED, MessageStatus.PROCESSED, MessageStatus.FAILED}
    ),
    MessageStatus.PROCESSED: frozenset(),
    MessageStatus.FAILED: frozenset({MessageStatus.QUEUED}),
}


def can_transition(current: MessageStatus, target: MessageStatus) -> bool:
    """Whether the queue lifecycle permits ``current -> target``."""
    return target in ALLOWED_TRANSITIONS[current]
