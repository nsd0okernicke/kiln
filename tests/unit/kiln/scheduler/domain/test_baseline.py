"""Tests for baseline-lock domain logic (issue #47, finding 2)."""

from __future__ import annotations

from kiln.scheduler.domain.baseline import (
    Baseline,
    BaselineEntry,
    compute_baseline_delta,
    format_baseline,
    handoff_blocked_by_baseline,
    parse_baseline,
)


def _entry(**overrides: object) -> BaselineEntry:
    fields = {"name": "test_foo.py::test_bar", "reason": "known flake", "backlog_task": ""}
    fields.update(overrides)
    return BaselineEntry(**fields)  # type: ignore[arg-types]


class TestBaselineEntry:
    def test_constructs_with_minimal_args(self):
        entry = BaselineEntry(name="test_x.py::test_y")
        assert entry.name == "test_x.py::test_y"
        assert entry.since  # auto-generated timestamp
        assert entry.reason == ""
        assert entry.backlog_task == ""

    def test_constructs_with_full_args(self):
        entry = BaselineEntry(
            name="test_z.py", since="2025-01-01T00:00:00Z", reason="needs DB", backlog_task="CAT-3"
        )
        assert entry.backlog_task == "CAT-3"


class TestBaseline:
    def test_empty_baseline(self):
        baseline = Baseline()
        assert baseline.names() == set()
        assert baseline.entry_for("anything") is None
        assert not baseline.has_backlog_for("anything")

    def test_entries_names(self):
        baseline = Baseline(entries=(_entry(name="a"), _entry(name="b")))
        assert baseline.names() == {"a", "b"}

    def test_entry_for_found(self):
        baseline = Baseline(entries=(_entry(name="foo"), _entry(name="bar")))
        entry = baseline.entry_for("foo")
        assert entry is not None
        assert entry.name == "foo"

    def test_entry_for_not_found(self):
        baseline = Baseline(entries=(_entry(name="foo"),))
        assert baseline.entry_for("nonexistent") is None

    def test_has_backlog_for_true(self):
        baseline = Baseline(entries=(_entry(name="foo", backlog_task="TASK-1"),))
        assert baseline.has_backlog_for("foo")

    def test_has_backlog_for_false(self):
        baseline = Baseline(entries=(_entry(name="foo", backlog_task=""),))
        assert not baseline.has_backlog_for("foo")


class TestParseBaseline:
    def test_parses_empty(self):
        baseline = parse_baseline("")
        assert len(baseline.entries) == 0

    def test_parses_comments_only(self):
        baseline = parse_baseline("# this is a comment\n# another comment")
        assert len(baseline.entries) == 0

    def test_parses_single_entry(self):
        text = "test_foo.py::test_bar\t2025-01-01T00:00:00Z\tknown\tTASK-1"
        baseline = parse_baseline(text)
        assert len(baseline.entries) == 1
        assert baseline.entries[0].name == "test_foo.py::test_bar"
        assert baseline.entries[0].reason == "known"
        assert baseline.entries[0].backlog_task == "TASK-1"

    def test_parses_multiple_entries(self):
        text = "test_a\t2025-01-01\treason1\t\n# comment\ntest_b\t2025-01-02\treason2\tTASK-2"
        baseline = parse_baseline(text)
        assert len(baseline.entries) == 2
        names = baseline.names()
        assert "test_a" in names
        assert "test_b" in names
        assert baseline.entry_for("test_b").backlog_task == "TASK-2"


class TestFormatBaseline:
    def test_roundtrips(self):
        original = (
            "test_a\t2025-01-01T00:00:00Z\treason1\t-\n"
            "test_b\t2025-01-02T00:00:00Z\treason2\tTASK-1\n"
        )
        baseline = parse_baseline(original)
        formatted = format_baseline(baseline)
        # Parse again
        parsed = parse_baseline(formatted)
        assert parsed.names() == {"test_a", "test_b"}
        assert parsed.entry_for("test_b").backlog_task == "TASK-1"


class TestComputeBaselineDelta:
    def test_no_failures_no_delta(self):
        baseline = Baseline(entries=(_entry(name="old_flake"),))
        delta = compute_baseline_delta(set(), baseline)
        assert delta["new_failures"] == []
        assert delta["missing_from_baseline"] == ["old_flake"]
        assert delta["expired_without_backlog"] == []

    def test_new_failure_detected(self):
        baseline = Baseline()
        delta = compute_baseline_delta({"new_test_failure"}, baseline)
        assert delta["new_failures"] == ["new_test_failure"]

    def test_all_baselined_still_failing(self):
        baseline = Baseline(entries=(_entry(name="known_failure", backlog_task="TASK-1"),))
        delta = compute_baseline_delta({"known_failure"}, baseline)
        assert delta["new_failures"] == []
        assert delta["expired_without_backlog"] == []

    def test_expired_without_backlog(self):
        baseline = Baseline(entries=(_entry(name="stale_failure", backlog_task=""),))
        delta = compute_baseline_delta({"stale_failure"}, baseline)
        assert "stale_failure" in delta["expired_without_backlog"]


class TestHandoffBlockedByBaseline:
    def test_no_failures_not_blocked(self):
        assert handoff_blocked_by_baseline(set(), None) is None

    def test_no_baseline_blocks(self):
        reason = handoff_blocked_by_baseline({"test_a"}, None)
        assert reason is not None
        assert "no baseline" in reason.lower()

    def test_new_failure_blocks(self):
        baseline = Baseline(entries=(_entry(name="known"),))
        reason = handoff_blocked_by_baseline({"known", "new_failure"}, baseline)
        assert reason is not None
        assert "new" in reason.lower() or "not in baseline" in reason.lower()

    def test_all_baselined_passes(self):
        baseline = Baseline(
            entries=(_entry(name="known", backlog_task="TASK-1"),)
        )
        assert handoff_blocked_by_baseline({"known"}, baseline) is None
