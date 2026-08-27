"""Scheduler domain values shared by application and infrastructure adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from .status_contract import WorkerResult

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


class InboundMessage(TypedDict):
    """The fields guaranteed by queue delivery operations."""

    id: str
    sender: str
    content: str
    priority: int


@dataclass(frozen=True)
class WorkerRequest:
    """One application-level request to execute a role worker."""

    prompt: str
    attempt: int = 1
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for one worker invocation, kept separate by billing category."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(frozen=True)
class WorkerInvocation:
    """Outcome of one worker execution, independent of the backend that produced it."""

    result: WorkerResult
    raw_output: str
    cost_usd: float = 0.0
    is_error: bool = False
    timed_out: bool = False
    detail: str = ""
    tokens: TokenUsage | None = None

    @property
    def is_done(self) -> bool:
        return self.result.is_done


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
