"""
Reading test reports off a real filesystem (issue #27).

Real files in `tmp_path`, never a mocked path object: every case here is a way the disk
disappoints — the file is not there, the directory is empty, the XML is truncated, the config
is not JSON — and a fake filesystem is exactly the thing that models those wrongly.

The rule under test throughout: `collect` never raises. This is a monitoring surface, and a
broken report must render as broken rather than take the endpoint down with it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from kiln.cockpit.application import test_metrics
from kiln.cockpit.infrastructure import test_reports

SUITE = (
    '<testsuite name="s" tests="{tests}" failures="{failures}" errors="0" skipped="0"'
    ' time="1.5">{cases}</testsuite>'
)
FAILING_CASE = '<testcase classname="C" name="broken"><failure/></testcase>'
COBERTURA = '<coverage line-rate="0.873"/>'


def write_suite(path, *, tests=2, failures=0, cases=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SUITE.format(tests=tests, failures=failures, cases=cases), encoding="utf-8")
    return path


@pytest.fixture
def config():
    return test_metrics.TestMetricsConfig(framework="pytest", junit="junit.xml")


class TestCollect:
    def test_reads_a_single_report(self, tmp_path, config):
        write_suite(tmp_path / "junit.xml")
        payload = test_reports.collect(config, root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_PASSED
        assert payload["tests"] == 2

    def test_relative_paths_resolve_from_the_project_root(self, tmp_path):
        """
        Not from the cockpit's cwd. The cockpit is launched from wherever the terminal
        backend happened to start it, which is not something a project's config can know.
        """
        write_suite(tmp_path / "build" / "junit.xml")
        config = test_metrics.TestMetricsConfig(junit="build/junit.xml")
        assert test_reports.collect(config, root=tmp_path)["tests"] == 2

    def test_an_absolute_path_is_honoured(self, tmp_path):
        report = write_suite(tmp_path / "elsewhere" / "junit.xml")
        config = test_metrics.TestMetricsConfig(junit=str(report))
        assert test_reports.collect(config, root=tmp_path / "unrelated")["tests"] == 2

    def test_a_directory_aggregates_every_xml_child(self, tmp_path):
        """Gradle and Maven write one file per test class."""
        write_suite(tmp_path / "results" / "a.xml", tests=3)
        write_suite(tmp_path / "results" / "b.xml", tests=4)
        config = test_metrics.TestMetricsConfig(junit="results")
        assert test_reports.collect(config, root=tmp_path)["tests"] == 7

    def test_a_directory_ignores_non_xml_siblings(self, tmp_path):
        write_suite(tmp_path / "results" / "a.xml", tests=3)
        (tmp_path / "results" / "notes.txt").write_text("ignore me", encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="results")
        assert test_reports.collect(config, root=tmp_path)["tests"] == 3

    def test_coverage_is_read_alongside(self, tmp_path):
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_text(COBERTURA, encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")
        assert test_reports.collect(config, root=tmp_path)["coverage"]["line_percent"] == 87.3

    def test_branch_coverage_and_measured_size_reach_the_payload(self, tmp_path):
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_text(
            '<coverage line-rate="0.9" lines-valid="200" lines-covered="180"'
            ' branches-valid="40" branches-covered="30" branch-rate="0.75"/>',
            encoding="utf-8",
        )
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")

        coverage = test_reports.collect(config, root=tmp_path)["coverage"]

        assert coverage == {
            "line_percent": 90.0,
            "lines_covered": 180,
            "lines_valid": 200,
            "branch_percent": 75.0,
            "branches_covered": 30,
            "branches_valid": 40,
        }

    def test_only_the_first_coverage_document_is_read(self, tmp_path):
        """
        Unlike JUnit and SARIF, coverage totals are ratios over a body of code: summing two
        of them produces a number that is not the coverage of anything. A directory holding
        several is read as the first by name, not merged into a fiction.
        """
        write_suite(tmp_path / "junit.xml")
        reports = tmp_path / "cov"
        reports.mkdir()
        (reports / "a.xml").write_text('<coverage line-rate="0.4"/>', encoding="utf-8")
        (reports / "b.xml").write_text('<coverage line-rate="0.8"/>', encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov")

        assert test_reports.collect(config, root=tmp_path)["coverage"]["line_percent"] == 40.0

    def test_failing_cases_are_named(self, tmp_path, config):
        write_suite(tmp_path / "junit.xml", tests=2, failures=1, cases=FAILING_CASE)
        payload = test_reports.collect(config, root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_FAILED
        assert payload["failed_names"] == ["C::broken"]


class TestDegradation:
    def test_nothing_configured_says_so(self, tmp_path):
        payload = test_reports.collect(test_metrics.TestMetricsConfig(), root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert "no test metrics configured" in payload["error"]

    def test_a_missing_report_names_the_path_it_looked_for(self, tmp_path, config):
        """The operator's next move is fixing the path, so the payload has to carry it."""
        payload = test_reports.collect(config, root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert "junit.xml" in payload["error"]

    def test_an_empty_directory_reads_as_missing(self, tmp_path):
        (tmp_path / "results").mkdir()
        config = test_metrics.TestMetricsConfig(junit="results")
        assert test_reports.collect(config, root=tmp_path)["status"] == (
            test_metrics.STATUS_UNAVAILABLE
        )

    def test_malformed_xml_degrades_instead_of_raising(self, tmp_path, config):
        (tmp_path / "junit.xml").write_text("<testsuite", encoding="utf-8")
        payload = test_reports.collect(config, root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert "malformed JUnit report" in payload["error"]

    def test_malformed_coverage_degrades_instead_of_raising(self, tmp_path):
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_text("<coverage", encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")
        payload = test_reports.collect(config, root=tmp_path)
        assert "malformed coverage report" in payload["error"]

    def test_an_oversized_report_is_skipped_rather_than_loaded(self, tmp_path, config, monkeypatch):
        """
        A wrong path can point at something enormous. Pulling it into memory every two
        seconds would make the monitoring surface the outage.
        """
        write_suite(tmp_path / "junit.xml")
        monkeypatch.setattr(test_reports, "MAX_REPORT_BYTES", 1)
        assert test_reports.collect(config, root=tmp_path)["tests"] is None

    def test_a_report_that_is_not_utf8_degrades(self, tmp_path, config):
        """
        A runner configured for a legacy encoding writes bytes this cannot decode. That is a
        report to skip, not an exception to raise on the poll.
        """
        (tmp_path / "junit.xml").write_bytes(
            '<testsuite name="caf\xe9" tests="1"/>'.encode("latin-1")
        )
        assert test_reports.collect(config, root=tmp_path)["tests"] is None

    def test_a_report_deleted_mid_read_does_not_raise(self, tmp_path):
        """
        The gap between listing a directory and stat-ing its files is real: a re-running
        suite deletes and rewrites them. A vanished file is skipped, not fatal.
        """
        assert test_reports._newest_mtime([tmp_path / "gone.xml"]) is None

    def test_an_unreadable_coverage_file_leaves_coverage_unknown(self, tmp_path):
        """
        The tests still have a verdict. One unreadable report should cost its own metric, not
        the whole panel — coverage is optional, and a pass/fail is the more useful half.
        """
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_bytes(
            '<coverage line-rate="0.9" name="caf\xe9"/>'.encode("latin-1")
        )
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")

        payload = test_reports.collect(config, root=tmp_path)

        assert payload["status"] == test_metrics.STATUS_PASSED
        assert payload["coverage"] is None

    def test_a_stale_report_is_flagged(self, tmp_path, config):
        write_suite(tmp_path / "junit.xml")
        later = datetime.now() + timedelta(minutes=config.max_age_minutes + 1)
        payload = test_reports.collect(config, root=tmp_path, now=later)
        assert payload["status"] == test_metrics.STATUS_STALE
        assert payload["stale"] is True


SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "ruff"}},
                "results": [{"ruleId": "F401", "level": "error", "message": {"text": "x"}}],
            }
        ],
    }
)


class TestLintReports:
    def test_reads_a_sarif_file(self, tmp_path):
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "lint.sarif").write_text(SARIF, encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", lint="lint.sarif")
        lint = test_reports.collect(config, root=tmp_path)["lint"]
        assert (lint["total"], lint["error"], lint["tools"]) == (1, 1, ["ruff"])

    def test_lint_alone_counts_as_configured(self, tmp_path):
        """A project may watch only its analyser; there is no requirement to run tests."""
        (tmp_path / "lint.sarif").write_text(SARIF, encoding="utf-8")
        config = test_metrics.TestMetricsConfig(lint="lint.sarif")
        assert test_reports.collect(config, root=tmp_path)["lint"]["total"] == 1

    def test_a_directory_of_sarif_is_read_whole(self, tmp_path):
        """
        A Java build may point PMD and SpotBugs at one folder. Note the extensions: SARIF is
        JSON, so a `*.xml` scan — which is right for JUnit — would find nothing here.
        """
        reports = tmp_path / "sarif"
        reports.mkdir()
        (reports / "pmd.sarif").write_text(SARIF, encoding="utf-8")
        (reports / "spotbugs.json").write_text(SARIF, encoding="utf-8")
        config = test_metrics.TestMetricsConfig(lint="sarif")
        assert test_reports.collect(config, root=tmp_path)["lint"]["total"] == 2

    def test_no_lint_configured_leaves_it_unknown_not_zero(self, tmp_path, config):
        """ "Nothing configured" and "an analyser that found nothing" must not look alike."""
        write_suite(tmp_path / "junit.xml")
        assert test_reports.collect(config, root=tmp_path)["lint"] is None

    def test_malformed_sarif_degrades_instead_of_raising(self, tmp_path):
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "lint.sarif").write_text("{not json", encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", lint="lint.sarif")
        payload = test_reports.collect(config, root=tmp_path)
        assert payload["status"] == test_metrics.STATUS_UNAVAILABLE
        assert "malformed SARIF report" in payload["error"]

    def test_sarif_that_is_json_but_not_sarif_degrades(self, tmp_path):
        """A wrong path pointing at some other JSON file must not crash the endpoint."""
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "lint.sarif").write_text('["not", "a", "sarif", "log"]', encoding="utf-8")
        config = test_metrics.TestMetricsConfig(junit="junit.xml", lint="lint.sarif")
        assert test_reports.collect(config, root=tmp_path)["status"] in {
            test_metrics.STATUS_PASSED,
            test_metrics.STATUS_UNAVAILABLE,
        }


class TestFreshness:
    def test_a_stale_junit_is_not_disguised_by_fresh_coverage(self, tmp_path):
        """
        Staleness is judged on the oldest report, not the newest. A coverage file rewritten a
        minute ago must not make a JUnit report from yesterday read as current — that is
        exactly the false reassurance the stale state exists to prevent.
        """
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_text(COBERTURA, encoding="utf-8")
        old = time.time() - 7200
        os.utime(tmp_path / "junit.xml", (old, old))
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")

        payload = test_reports.collect(config, root=tmp_path)

        assert payload["status"] == test_metrics.STATUS_STALE

    def test_the_displayed_age_is_the_oldest_contributing_report(self, tmp_path):
        """The age and stale verdict describe the same combined report set."""
        write_suite(tmp_path / "junit.xml")
        (tmp_path / "cov.xml").write_text(COBERTURA, encoding="utf-8")
        old = time.time() - 7200
        os.utime(tmp_path / "junit.xml", (old, old))
        config = test_metrics.TestMetricsConfig(junit="junit.xml", coverage="cov.xml")

        updated = datetime.fromisoformat(test_reports.collect(config, root=tmp_path)["updated_at"])

        assert datetime.now() - updated > timedelta(hours=1)


class TestReportAge:
    def test_age_comes_from_the_newest_file_in_a_directory(self, tmp_path):
        """
        With one file per class the oldest says when the run started and the newest when it
        finished; "updated 3m ago" should mean the finish.
        """
        old = write_suite(tmp_path / "results" / "a.xml")
        write_suite(tmp_path / "results" / "b.xml")
        stale_time = time.time() - 3600
        os.utime(old, (stale_time, stale_time))
        config = test_metrics.TestMetricsConfig(junit="results")
        updated = datetime.fromisoformat(test_reports.collect(config, root=tmp_path)["updated_at"])
        assert datetime.now() - updated < timedelta(minutes=5)


class TestConfigLocation:
    def test_the_config_lives_under_the_state_directory(self, tmp_path):
        """
        One location, chosen deliberately: `.kiln/test-metrics.json`. There is no cascade and
        no project-root fallback, so a file left at the old path is simply not found rather
        than half-working.
        """
        assert test_reports.config_path(tmp_path) == tmp_path / ".kiln" / "test-metrics.json"

    def test_a_config_at_the_project_root_is_not_picked_up(self, tmp_path):
        (tmp_path / "kiln.test-metrics.json").write_text(
            json.dumps({"reports": {"junit": "j.xml"}}), encoding="utf-8"
        )
        assert test_reports.load_config(test_reports.config_path(tmp_path)) is None


class TestLoadConfig:
    def test_reads_the_documented_file(self, tmp_path):
        path = test_reports.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"framework": "pytest", "reports": {"junit": "j.xml"}}), encoding="utf-8"
        )
        config = test_reports.load_config(path)
        assert config is not None and config.junit == "j.xml"

    def test_an_absent_file_means_the_feature_is_off(self, tmp_path):
        assert test_reports.load_config(tmp_path / "nope.json") is None

    def test_malformed_json_is_reported_not_read_as_absent(self, tmp_path):
        """
        A file with a typo in it is not a missing file. Conflating them told the operator to
        "create one", sending them to write something already sitting on disk.
        """
        path = test_reports.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(test_reports.ReportError) as raised:
            test_reports.load_config(path)

        assert test_reports.CONFIG_DISPLAY_PATH in str(raised.value)

    def test_a_json_scalar_is_rejected(self, tmp_path):
        path = test_reports.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"pytest"', encoding="utf-8")

        with pytest.raises(test_reports.ReportError):
            test_reports.load_config(path)
