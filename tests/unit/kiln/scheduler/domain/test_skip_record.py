"""Tests for machine-readable skip records (issue #47, finding 5)."""

from __future__ import annotations

from kiln.scheduler.domain.skip_record import (
    REASON_CONTAINER_UNAVAILABLE,
    REASON_INFRA_ONLY,
    REASON_NO_MUTATION_TARGETS,
    SkipRecord,
    format_skip_record,
    parse_skip_line,
    skip_budget_exceeded,
)


class TestSkipRecord:
    def test_constructs_with_minimal_args(self):
        record = SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS)
        assert record.gate == "mutation"
        assert record.reason == REASON_NO_MUTATION_TARGETS
        assert record.key == f"mutation:{REASON_NO_MUTATION_TARGETS}"
        assert record.detail == ""
        assert record.timestamp  # auto-generated

    def test_constructs_with_all_args(self):
        record = SkipRecord(
            gate="acceptance",
            reason=REASON_CONTAINER_UNAVAILABLE,
            detail="Docker daemon not running",
            role="architect",
        )
        assert record.key == f"acceptance:{REASON_CONTAINER_UNAVAILABLE}"
        assert record.role == "architect"

    def test_infra_only_skip(self):
        record = SkipRecord(gate="mutation", reason=REASON_INFRA_ONLY, role="coder")
        assert record.key == f"mutation:{REASON_INFRA_ONLY}"


class TestFormatSkipRecord:
    def test_format_includes_all_fields(self):
        record = SkipRecord(
            gate="coverage",
            reason=REASON_INFRA_ONLY,
            detail="no src changes",
            role="refactorer",
        )
        line = format_skip_record(record)
        assert line.startswith("GATE_SKIP:")
        assert "gate=coverage" in line
        assert f"reason={REASON_INFRA_ONLY}" in line
        assert "detail=no src changes" in line
        assert "role=refactorer" in line

    def test_format_minimal(self):
        record = SkipRecord(gate="lint", reason="tool_unavailable")
        line = format_skip_record(record)
        assert line.startswith("GATE_SKIP:")
        assert "gate=lint" in line


class TestParseSkipLine:
    def test_parse_full_line(self):
        line = "GATE_SKIP: gate=mutation reason=no_mutation_targets role=architect detail=-"
        record = parse_skip_line(line)
        assert record is not None
        assert record.gate == "mutation"
        assert record.reason == "no_mutation_targets"
        assert record.role == "architect"

    def test_parse_minimal_line(self):
        line = "GATE_SKIP: gate=lint reason=tool_unavailable role=coder detail=-"
        record = parse_skip_line(line)
        assert record is not None
        assert record.gate == "lint"

    def test_parse_non_skip_line(self):
        assert parse_skip_line("normal handoff text") is None

    def test_parse_empty_line(self):
        assert parse_skip_line("") is None

    def test_parse_invalid_format(self):
        line = "GATE_SKIP: not=properly=formatted"
        record = parse_skip_line(line)
        assert record is not None
        assert record.gate == "unknown"


class TestSkipBudgetExceeded:
    def test_empty_records(self):
        assert skip_budget_exceeded([]) == []

    def test_no_exceeded(self):
        records = [
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
        ]
        assert skip_budget_exceeded(records, budget=2) == []

    def test_one_exceeded(self):
        records = [
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
        ]
        exceeded = skip_budget_exceeded(records, budget=2)
        assert f"mutation:{REASON_NO_MUTATION_TARGETS}" in exceeded

    def test_different_gates_independent(self):
        records = [
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
            SkipRecord(gate="acceptance", reason=REASON_CONTAINER_UNAVAILABLE),
        ]
        exceeded = skip_budget_exceeded(records, budget=1)
        mutation_key = f"mutation:{REASON_NO_MUTATION_TARGETS}"
        acceptance_key = f"acceptance:{REASON_CONTAINER_UNAVAILABLE}"
        assert mutation_key in exceeded
        assert acceptance_key not in exceeded

    def test_different_reasons_independent(self):
        records = [
            SkipRecord(gate="mutation", reason=REASON_NO_MUTATION_TARGETS),
            SkipRecord(gate="mutation", reason=REASON_INFRA_ONLY),
        ]
        assert skip_budget_exceeded(records, budget=2) == []
