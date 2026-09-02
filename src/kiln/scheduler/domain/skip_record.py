"""Machine-readable gate skip records (issue #47, finding 5).

Every quality gate that a role chooses to skip — rather than run — must emit a machine-
readable record explaining why. The scheduler then refuses handoff when the same gate has
been skipped N cycles running for the same reason.

This is pure domain: the record is produced by a role, consumed by the handoff guard, and
persisted to the message queue by infrastructure. No I/O happens here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

#: The number of consecutive skips of the same gate+reason before handoff is refused.
DEFAULT_SKIP_BUDGET = 2

#: Standard reason codes. Roles should prefer these to freeform text wherever possible.
REASON_NO_MUTATION_TARGETS = "no_mutation_targets"
REASON_INFRA_ONLY = "infrastructure_only_changes"
REASON_CONTAINER_UNAVAILABLE = "container_unavailable"
REASON_NO_ACCEPTANCE_SUITE = "no_acceptance_suite"
REASON_MANUAL_INSPECTION = "manual_inspection_substituted"
REASON_FILE_NOT_FOUND = "file_not_found"
REASON_TOOL_UNAVAILABLE = "tool_unavailable"


@dataclass(frozen=True)
class SkipRecord:
    """One instance of a gate being skipped rather than run."""

    #: Which gate was skipped: ``"mutation"``, ``"acceptance"``, ``"coverage"``, etc.
    gate: str
    #: Machine-readable reason code.
    reason: str
    #: Human-readable explanation (optional).
    detail: str = ""
    #: When the skip was recorded.
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    #: The role that skipped the gate.
    role: str = ""

    @property
    def key(self) -> str:
        """Grouping key for consecutive-skip counting: ``f\"{gate}:{reason}\"``."""
        return f"{self.gate}:{self.reason}"


def format_skip_record(skip: SkipRecord) -> str:
    """One machine-readable line for handoff prose or a DB column."""
    return f"GATE_SKIP: gate={skip.gate} reason={skip.reason} role={skip.role} detail={skip.detail or '-'}"


def parse_skip_line(line: str) -> SkipRecord | None:
    """Parse one ``GATE_SKIP:`` line back to a SkipRecord, or None if it doesn't match."""
    line = line.strip()
    if not line.startswith("GATE_SKIP:"):
        return None
    payload = line[len("GATE_SKIP:"):].strip()
    parts = {}
    for segment in payload.split():
        if "=" in segment:
            key, _, value = segment.partition("=")
            parts[key] = value
    return SkipRecord(
        gate=parts.get("gate", "unknown"),
        reason=parts.get("reason", "unknown"),
        detail=parts.get("detail", ""),
        role=parts.get("role", ""),
    )


def skip_budget_exceeded(skip_records: list[SkipRecord], budget: int = DEFAULT_SKIP_BUDGET) -> list[str]:
    """
    Return the keys of (gate, reason) pairs that exceed the skip budget.

    Counts how many times each (gate, reason) pair appears in the provided records.
    A pair whose count exceeds `budget` is returned. The budget represents how many
    consecutive cycles a gate may be skipped for the same reason before handoff is
    refused.
    """
    from collections import Counter
    counts = Counter(record.key for record in skip_records)
    return [key for key, count in counts.items() if count > budget]
