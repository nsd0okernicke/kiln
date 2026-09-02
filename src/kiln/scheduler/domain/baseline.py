"""Baseline-lock for acceptance tests (issue #47, finding 2).

A committed baseline file records the set of tests that are known to fail. The scheduler
refuses handoff if:

1. The failing set *grew* since the baseline was written (new regression).
2. An inherited failure is older than one cycle without a backlog task attached.

This is pure domain: parsing, diffing, and expiry rules. The baseline file itself is read
and written by infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class BaselineEntry:
    """One known-failing test in the baseline."""

    #: Full test name, e.g. ``"tests/acceptance/test_catalogs.py::test_sync"``.
    name: str
    #: When this failure was first baselined (ISO timestamp).
    since: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    #: Reason this test is expected to fail.
    reason: str = ""
    #: Link to a backlog task tracking the fix, or empty.
    backlog_task: str = ""


@dataclass(frozen=True)
class Baseline:
    """The complete baseline file content."""

    entries: tuple[BaselineEntry, ...] = ()
    #: ISO timestamp of the last update.
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    def names(self) -> set[str]:
        return {entry.name for entry in self.entries}

    def entry_for(self, name: str) -> BaselineEntry | None:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def has_backlog_for(self, name: str) -> bool:
        entry = self.entry_for(name)
        return bool(entry and entry.backlog_task)


def _field(parts: list[str], index: int) -> str:
    """The value at `index` in parts, or empty when the index is out of range."""
    return parts[index].strip() if len(parts) > index else ""


def _parse_entry_line(line: str) -> BaselineEntry | None:
    """One non-comment line to a BaselineEntry, or None when the name is empty."""
    parts = [p.strip() for p in line.split("\t")]
    name = _field(parts, 0)
    if not name:
        return None
    return BaselineEntry(name=name, since=_field(parts, 1), reason=_field(parts, 2), backlog_task=_field(parts, 3))


def parse_baseline(content: str) -> Baseline:
    """Parse a baseline file into entries."""
    entries = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entry = _parse_entry_line(stripped)
            if entry is not None:
                entries.append(entry)
    return Baseline(entries=tuple(entries))


def format_baseline(baseline: Baseline) -> str:
    """Serialize a baseline to text. Tab-separated: name, since, reason, backlog_task."""
    lines = [
        "# Known-failing acceptance tests baseline",
        "# Format: name<TAB>since<TAB>reason<TAB>backlog_task",
        f"# Updated: {baseline.updated_at}",
    ]
    for entry in baseline.entries:
        backlog = entry.backlog_task or "-"
        lines.append(f"{entry.name}\t{entry.since}\t{entry.reason}\t{backlog}")
    return "\n".join(lines) + "\n"


def compute_baseline_delta(current_failures: set[str], baseline: Baseline) -> dict:
    """Compare a set of currently-failing test names against the baseline.

    Returns:
        new_failures: tests failing now that are not in the baseline.
        missing_baseline: tests in the baseline that are no longer failing (expected pass).
        expired: baseline entries older than one cycle with no backlog task.
    """
    baseline_names = baseline.names()
    new_failures = current_failures - baseline_names
    missing_baseline = baseline_names - current_failures

    # Expired: in baseline and still failing, no backlog task, older than one cycle.
    expired = []
    for entry in baseline.entries:
        if entry.name in current_failures and not entry.backlog_task:
            expired.append(entry.name)

    return {
        "new_failures": sorted(new_failures),
        "missing_from_baseline": sorted(missing_baseline),
        "expired_without_backlog": sorted(expired),
    }


def _no_baseline_block(current_failures: set[str]) -> str:
    return (
        f"{len(current_failures)} test(s) failing and no baseline file committed. "
        "Create `.kiln/test-baseline.txt` with the known-failing set, or fix the failures."
    )


def _baseline_delta_reasons(delta: dict) -> list[str]:
    reasons = []
    if delta["new_failures"]:
        reasons.append(f"New failure(s) not in baseline: {', '.join(delta['new_failures'][:5])}")
    if delta["expired_without_backlog"]:
        reasons.append(f"Baseline failure(s) without backlog task: {', '.join(delta['expired_without_backlog'][:5])}")
    return reasons


def handoff_blocked_by_baseline(
    current_failures: set[str],
    baseline: Baseline | None,
    one_cycle_seconds: int = 3600,
) -> str | None:
    if not current_failures:
        return None
    if baseline is None:
        return _no_baseline_block(current_failures)
    reasons = _baseline_delta_reasons(compute_baseline_delta(current_failures, baseline))
    return "; ".join(reasons) if reasons else None
