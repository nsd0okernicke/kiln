"""
Pure test-health model: parsing, aggregation, staleness and payload shape (issue #27).

Document text in, dict out — no filesystem here. The report shapes are copied from real
output (pytest's `--junitxml`, coverage.py's Cobertura XML, a Gradle per-class file), because
a parser tested only against XML the test itself invented tends to be a parser for XML nobody
writes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from kiln.cockpit.application import test_metrics

PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="0" skipped="5"
 tests="2018" time="130.834" timestamp="2026-08-24T12:30:15" hostname="runner"/></testsuites>
"""

GRADLE_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="CatalogServiceTest" tests="3" skipped="0" failures="1" errors="0" time="0.42">
  <testcase name="findsByAuthor" classname="CatalogServiceTest" time="0.1"/>
  <testcase name="rejectsBlankTitle" classname="CatalogServiceTest" time="0.2">
    <failure message="expected blank to be rejected">AssertionError</failure>
  </testcase>
</testsuite>
"""

COBERTURA = (
    '<coverage version="7.15.4" lines-valid="6228" lines-covered="6054" line-rate="0.9721"'
    ' branches-valid="1558" branches-covered="1438" branch-rate="0.923"/>'
)

NOW = datetime(2026, 8, 24, 12, 0, 0)


class TestParseJunit:
    def test_reads_a_testsuites_wrapper(self):
        """pytest, Jest and the .NET exporters all wrap suites in <testsuites>."""
        result = test_metrics.parse_junit(PYTEST_JUNIT)
        assert result["tests"] == 2018
        assert result["skipped"] == 5
        assert result["failed"] == 0
        assert result["duration_sec"] == pytest.approx(130.834)

    def test_reads_a_bare_testsuite_root(self):
        """Gradle and older Ant reports have no wrapper; `iter` must still find the root."""
        assert test_metrics.parse_junit(GRADLE_JUNIT)["tests"] == 3

    def test_derives_passed_because_junit_has_no_such_attribute(self):
        assert test_metrics.parse_junit(PYTEST_JUNIT)["passed"] == 2018 - 5

    def test_counts_errors_as_failures(self):
        """An errored case did not pass; splitting the two would understate the damage."""
        xml = '<testsuite tests="4" failures="1" errors="2" skipped="0" time="1"/>'
        assert test_metrics.parse_junit(xml)["failed"] == 3

    def test_names_failing_cases_with_their_class(self):
        assert test_metrics.parse_junit(GRADLE_JUNIT)["failed_names"] == [
            "CatalogServiceTest::rejectsBlankTitle"
        ]

    def test_missing_numeric_attributes_read_as_zero(self):
        """A suite element with no counts is empty, not malformed."""
        assert test_metrics.parse_junit("<testsuite/>")["tests"] == 0

    def test_never_reports_a_negative_pass_count(self):
        """
        A malformed report whose parts exceed its total must not produce -1 passed.

        Showing a nonsense negative would be worse than showing zero, and the failure count
        beside it already tells the operator the run went badly.
        """
        xml = '<testsuite tests="1" failures="5" errors="0" skipped="0" time="1"/>'
        assert test_metrics.parse_junit(xml)["passed"] == 0

    def test_malformed_xml_raises_for_the_caller_to_translate(self):
        with pytest.raises(Exception):  # noqa: B017 - ET.ParseError, caught by the reader
            test_metrics.parse_junit("<testsuite")


class TestMergeJunit:
    def test_sums_counts_across_per_class_files(self):
        """Maven and Gradle write one file per class, so a directory is the normal case."""
        one = test_metrics.parse_junit(GRADLE_JUNIT)
        merged = test_metrics.merge_junit([one, one])
        assert (merged["tests"], merged["failed"]) == (6, 2)
        assert merged["duration_sec"] == pytest.approx(0.84)

    def test_concatenates_failing_names_in_file_order(self):
        one = test_metrics.parse_junit(GRADLE_JUNIT)
        assert len(test_metrics.merge_junit([one, one])["failed_names"]) == 2

    def test_no_reports_is_zeroed_rather_than_absent(self):
        """The caller decides that nothing was read; merge itself has nothing to say."""
        assert test_metrics.merge_junit([])["tests"] == 0


class TestParseCobertura:
    def test_reads_line_rate_as_a_percentage(self):
        assert test_metrics.parse_cobertura(COBERTURA)["line_percent"] == 97.21

    def test_absent_line_rate_is_unknown_not_zero(self):
        """An unrecognised dialect must not be reported as 0% covered."""
        assert test_metrics.parse_cobertura("<coverage/>") is None

    def test_reads_the_measured_size_from_the_coverage_tool(self):
        """
        `lines-valid` is executable statements, counted by the tool that knows the language.
        It is what lets the panel state a size in any ecosystem without Kiln parsing source.
        """
        coverage = test_metrics.parse_cobertura(COBERTURA)
        assert coverage["lines_valid"] == 6228
        assert coverage["lines_covered"] == 6054

    def test_reads_branch_coverage_when_it_was_measured(self):
        coverage = test_metrics.parse_cobertura(COBERTURA)
        assert coverage["branch_percent"] == 92.3
        assert coverage["branches_covered"] == 1438
        assert coverage["branches_valid"] == 1558

    def test_unmeasured_branches_are_unknown_not_zero_percent(self):
        """
        What coverage.py writes without `--cov-branch`, and JaCoCo for branchless code:
        `branches-valid="0"` beside `branch-rate="0"`. Reporting that as 0% branch coverage
        would turn a setting nobody switched on into an apparent catastrophe.
        """
        document = '<coverage line-rate="1" branches-valid="0" branch-rate="0"/>'
        coverage = test_metrics.parse_cobertura(document)
        assert coverage["line_percent"] == 100.0
        assert coverage["branch_percent"] is None
        assert coverage["branches_covered"] is None
        assert coverage["branches_valid"] is None

    def test_a_document_without_any_branch_attributes_reads_the_same_way(self):
        """Istanbul and older exporters omit them entirely rather than writing zeroes."""
        assert test_metrics.parse_cobertura('<coverage line-rate="0.5"/>')["branch_percent"] is None


#: Shape verified against a real `ruff check --output-format=sarif` run.
RUFF_SARIF = json.dumps(
    {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "ruff", "version": "0.16.1", "rules": []}},
                "results": [
                    {"ruleId": "F401", "level": "error", "message": {"text": "unused import"}},
                    {"ruleId": "E501", "level": "warning", "message": {"text": "line too long"}},
                ],
            }
        ],
    }
)

#: A Java build merging two analysers into one document, which SARIF models as two runs.
JAVA_SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "PMD",
                        # No level on the result below; it resolves through this rule.
                        "rules": [
                            {"id": "UnusedImports", "defaultConfiguration": {"level": "note"}}
                        ],
                    }
                },
                "results": [{"ruleId": "UnusedImports", "message": {"text": "unused"}}],
            },
            {
                "tool": {"driver": {"name": "SpotBugs"}},
                "results": [{"ruleId": "NP_NULL", "level": "error", "message": {"text": "npe"}}],
            },
        ],
    }
)


class TestParseSarif:
    def test_counts_by_level(self):
        lint = test_metrics.parse_sarif(RUFF_SARIF)
        assert (lint["error"], lint["warning"], lint["total"]) == (1, 1, 2)

    def test_names_the_tool_from_the_document(self):
        """Read by format, not by tool — the analyser identifies itself."""
        assert test_metrics.parse_sarif(RUFF_SARIF)["tools"] == ["ruff"]

    def test_a_clean_run_is_zero_not_unknown(self):
        """An analyser that found nothing did run; that is a fact worth showing."""
        clean = json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {}}, "results": []}]})
        assert test_metrics.parse_sarif(clean)["total"] == 0

    def test_several_runs_in_one_document_are_summed(self):
        """How a Java build merges PMD and SpotBugs into a single file."""
        lint = test_metrics.parse_sarif(JAVA_SARIF)
        assert lint["total"] == 2
        assert lint["tools"] == ["PMD", "SpotBugs"]

    def test_a_result_without_a_level_falls_back_to_its_rule(self):
        """SARIF §3.27.10: the result's level, then the rule's default, then warning."""
        assert test_metrics.parse_sarif(JAVA_SARIF)["note"] == 1

    def test_a_result_with_no_level_anywhere_is_a_warning(self):
        """The spec's default. Not error — an omitted field is not a reported failure."""
        doc = json.dumps(
            {"version": "2.1.0", "runs": [{"tool": {"driver": {}}, "results": [{"ruleId": "X"}]}]}
        )
        assert test_metrics.parse_sarif(doc)["warning"] == 1

    def test_non_failing_results_are_not_findings(self):
        """
        CodeQL and some Java analysers emit `kind: "pass"` rows in the same document.
        Counting those as problems would report a clean run as dozens of violations.
        """
        doc = json.dumps(
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {"driver": {}},
                        "results": [
                            {"ruleId": "A", "kind": "pass", "level": "none"},
                            {"ruleId": "B", "kind": "fail", "level": "error"},
                        ],
                    }
                ],
            }
        )
        assert test_metrics.parse_sarif(doc)["total"] == 1

    def test_a_rule_declaring_no_default_level_is_skipped(self):
        """
        Real PMD and SpotBugs documents list rules with no `defaultConfiguration` at all.
        Such a rule contributes no fallback, so its results take the spec's warning default.
        """
        doc = json.dumps(
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "name": "PMD",
                                "rules": [
                                    {"id": "NoDefault", "shortDescription": {"text": "x"}},
                                    {"defaultConfiguration": {"level": "error"}},
                                ],
                            }
                        },
                        "results": [{"ruleId": "NoDefault", "message": {"text": "y"}}],
                    }
                ],
            }
        )
        assert test_metrics.parse_sarif(doc)["warning"] == 1

    def test_one_tool_named_twice_in_a_document_is_listed_once(self):
        """A build may split one analyser across runs; the panel should not say it twice."""
        doc = json.dumps(
            {
                "version": "2.1.0",
                "runs": [
                    {"tool": {"driver": {"name": "PMD"}}, "results": []},
                    {"tool": {"driver": {"name": "PMD"}}, "results": []},
                ],
            }
        )
        assert test_metrics.parse_sarif(doc)["tools"] == ["PMD"]

    def test_missing_arrays_are_empty_rather_than_fatal(self):
        """Every SARIF array is optional; an absent one is not a malformed document."""
        assert test_metrics.parse_sarif(json.dumps({"version": "2.1.0"}))["total"] == 0

    def test_malformed_json_raises_for_the_caller_to_translate(self):
        with pytest.raises(json.JSONDecodeError):
            test_metrics.parse_sarif("{not json")


class TestMergeSarif:
    def test_sums_across_documents(self):
        one = test_metrics.parse_sarif(RUFF_SARIF)
        assert test_metrics.merge_sarif([one, one])["total"] == 4

    def test_deduplicates_tool_names(self):
        """Two files from the same analyser name it once, not twice."""
        one = test_metrics.parse_sarif(RUFF_SARIF)
        assert test_metrics.merge_sarif([one, one])["tools"] == ["ruff"]

    def test_combines_tools_across_ecosystems(self):
        merged = test_metrics.merge_sarif(
            [test_metrics.parse_sarif(RUFF_SARIF), test_metrics.parse_sarif(JAVA_SARIF)]
        )
        assert merged["tools"] == ["ruff", "PMD", "SpotBugs"]


class TestStaleness:
    def test_a_report_inside_the_window_is_fresh(self):
        recent = NOW - timedelta(minutes=5)
        assert not test_metrics.is_stale(recent, now=NOW, max_age_minutes=30)

    def test_a_report_past_the_window_is_stale(self):
        old = NOW - timedelta(minutes=31)
        assert test_metrics.is_stale(old, now=NOW, max_age_minutes=30)

    def test_a_future_timestamp_is_never_stale(self):
        """
        Clock skew between a container that wrote the report and the host reading it is
        routine. Calling a report written seconds ago "stale" is the more confusing error.
        """
        assert not test_metrics.is_stale(NOW + timedelta(hours=1), now=NOW, max_age_minutes=30)

    def test_no_timestamp_is_not_stale(self):
        assert not test_metrics.is_stale(None, now=NOW, max_age_minutes=30)


class TestBuildPayload:
    def _config(self, **kwargs):
        return test_metrics.TestMetricsConfig(framework="pytest", junit="j.xml", **kwargs)

    def test_a_clean_run_is_passed(self):
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=test_metrics.parse_junit(PYTEST_JUNIT),
            coverage=test_metrics.parse_cobertura(COBERTURA),
            lint=None,
            freshness=[NOW],
            now=NOW,
        )
        assert payload["status"] == test_metrics.STATUS_PASSED
        assert payload["coverage"]["line_percent"] == 97.21
        assert payload["coverage"]["branch_percent"] == 92.3

    def test_any_failure_is_failed(self):
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=test_metrics.parse_junit(GRADLE_JUNIT),
            coverage=None,
            lint=None,
            freshness=[NOW],
            now=NOW,
        )
        assert payload["status"] == test_metrics.STATUS_FAILED

    def test_staleness_outranks_a_passing_result(self):
        """
        The whole point of the stale state: a green run from an hour ago describes code that
        has since moved on, and reporting it as "passed" is exactly the false reassurance an
        operator must not be given. The counts stay so the panel can show what it said.
        """
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=test_metrics.parse_junit(PYTEST_JUNIT),
            coverage=None,
            lint=None,
            freshness=[NOW - timedelta(hours=2)],
            now=NOW,
        )
        assert payload["status"] == test_metrics.STATUS_STALE
        assert payload["tests"] == 2018

    def test_displayed_age_is_the_oldest_contributing_report(self):
        old_lint = NOW - timedelta(hours=2)
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=test_metrics.parse_junit(PYTEST_JUNIT),
            coverage=None,
            lint=None,
            freshness=[NOW, old_lint],
            now=NOW,
        )
        assert payload["status"] == test_metrics.STATUS_STALE
        assert payload["updated_at"] == old_lint.isoformat(timespec="seconds")

    def test_coverage_without_junit_gives_no_verdict(self):
        """There is a number to show but no pass/fail to claim."""
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=None,
            coverage=test_metrics.parse_cobertura('<coverage line-rate="0.5"/>'),
            lint=None,
            freshness=[NOW],
            now=NOW,
        )
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert payload["coverage"]["line_percent"] == 50.0
        assert payload["tests"] is None

    def test_unknown_counts_are_none_never_zero(self):
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=None,
            coverage=None,
            lint=None,
            freshness=[],
            now=NOW,
        )
        assert [payload[key] for key in ("tests", "passed", "failed", "skipped")] == [None] * 4

    def test_failing_names_are_capped(self):
        """A wall of names helps nobody; the count beside them says how many were elided."""
        junit = test_metrics.parse_junit(GRADLE_JUNIT)
        junit["failed_names"] = [f"t{index}" for index in range(50)]
        payload = test_metrics.build_payload(
            config=self._config(),
            junit=junit,
            coverage=None,
            lint=None,
            freshness=[NOW],
            now=NOW,
        )
        assert len(payload["failed_names"]) == test_metrics.FAILED_NAME_LIMIT

    def test_the_error_state_carries_the_same_keys(self):
        """
        A consumer that must ask which shape it received will eventually forget to. The page
        reads `stale` and `max_age_minutes` unconditionally.
        """
        good = test_metrics.build_payload(
            config=self._config(),
            junit=test_metrics.parse_junit(PYTEST_JUNIT),
            coverage=None,
            lint=None,
            freshness=[NOW],
            now=NOW,
        )
        assert test_metrics.unavailable("nope", config=self._config()).keys() == good.keys()


class TestConfigFromMapping:
    def test_reads_the_documented_shape(self):
        config = test_metrics.config_from_mapping(
            {
                "framework": "pytest",
                "command": "python -m pytest --junitxml=.kiln/reports/junit.xml",
                "verificationRole": "architect",
                "reports": {"junit": ".kiln/reports/junit.xml", "coverage": "cov.xml"},
                "maxAgeMinutes": 15,
            }
        )
        assert (config.framework, config.junit, config.coverage) == (
            "pytest",
            ".kiln/reports/junit.xml",
            "cov.xml",
        )
        assert config.max_age_minutes == 15
        assert config.command.startswith("python -m pytest")
        assert config.verification_role == "architect"

    def test_unknown_keys_are_ignored_rather_than_fatal(self):
        """The schema is explicitly open; a file written for a later Kiln must still load."""
        config = test_metrics.config_from_mapping(
            {"framework": "pytest", "perRole": True, "reports": {"junit": "j.xml"}}
        )
        assert config.junit == "j.xml"

    def test_a_missing_reports_block_is_unconfigured(self):
        assert not test_metrics.config_from_mapping({"framework": "pytest"}).configured

    def test_coverage_alone_counts_as_configured(self):
        assert test_metrics.config_from_mapping({"reports": {"coverage": "c.xml"}}).configured

    @pytest.mark.parametrize("value", [0, -5, "soon", None])
    def test_an_unusable_max_age_falls_back_to_the_default(self, value):
        """A zero or negative window would mark every report stale the instant it landed."""
        config = test_metrics.config_from_mapping({"maxAgeMinutes": value})
        assert config.max_age_minutes == test_metrics.DEFAULT_MAX_AGE_MINUTES
