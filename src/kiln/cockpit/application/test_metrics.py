"""
Framework-neutral test health, derived from report files a project already writes.

The cockpit *reads* reports; it never runs the test command (issue #27). Executing a suite on
a two-second poll would burn CPU, rewrite build output and race a running swarm, so the
`command` a project configures is recorded for other Kiln workflows and deliberately unused
here.

Everything in this module is pure: parsing takes document text, not paths. The filesystem
half lives in `cockpit.infrastructure.test_reports`, which is what keeps a malformed report a
parsing question rather than an I/O one -- and lets the awkward cases (no `tests` attribute,
a suite that never ran, a clock skewed into the future) be tested without writing files.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"
STATUS_UNAVAILABLE = "unavailable"

DEFAULT_MAX_AGE_MINUTES = 30

#: Cap on named failures carried in the payload. A wall of four hundred names helps nobody,
#: and the count beside them already says how many were elided.
FAILED_NAME_LIMIT = 10


@dataclass(frozen=True)
class TestMetricsConfig:
    """
    Project-level description of where reports live and how to read them.

    Loaded once at startup from `.kiln/test-metrics.json`, never re-read on the poll: the
    reports change, the configuration does not.
    """

    framework: str = ""
    junit: str = ""
    coverage: str = ""
    #: SARIF 2.1.0, from whichever analyser the project runs. Read by format, not by tool.
    lint: str = ""
    #: Recorded for other Kiln workflows. The cockpit must never run it -- see module docstring.
    command: str = ""
    #: Scheduler role that enforces ``command`` before handing work onward.
    verification_role: str = ""
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES

    @property
    def configured(self) -> bool:
        """True when there is at least one report to read. Any one of them is enough."""
        return bool(self.junit or self.coverage or self.lint)


def config_from_mapping(payload: dict) -> TestMetricsConfig:
    """
    Build a config from parsed JSON, ignoring keys this version does not know.

    Forgiving by design: the issue leaves the schema open, so a project that has written a
    richer file for a later Kiln should still get the fields this version understands rather
    than an error.
    """
    reports = payload.get("reports")
    reports = reports if isinstance(reports, dict) else {}
    return TestMetricsConfig(
        framework=_text(payload, "framework"),
        junit=_text(reports, "junit"),
        coverage=_text(reports, "coverage"),
        lint=_text(reports, "lint"),
        command=_text(payload, "command"),
        verification_role=_text(payload, "verificationRole"),
        max_age_minutes=_positive_int(
            payload.get("maxAgeMinutes"), default=DEFAULT_MAX_AGE_MINUTES
        ),
    )


def _text(source: dict, key: str) -> str:
    """A missing, null or non-string value all read as "not set"."""
    return str(source.get(key) or "")


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _attr_int(element: ET.Element, name: str) -> int:
    """A missing numeric attribute means zero; a present but unreadable one is a bad report."""
    raw = element.get(name)
    return 0 if raw is None else int(raw)


def _attr_float(element: ET.Element, name: str) -> float:
    raw = element.get(name)
    return 0.0 if raw is None else float(raw)


def parse_junit(text: str) -> dict:
    """
    Totals and failing-test names from one JUnit XML document.

    Handles both shapes in the wild: a `<testsuites>` wrapper (what pytest, Jest and the
    .NET exporters emit) and a bare `<testsuite>` root (what some Gradle and older Ant
    reports emit). `iter` yields the root itself when it matches, so both collapse to the
    same walk.

    `passed` is derived rather than read -- JUnit has no passed attribute, only a total and
    the three ways a case can not pass.
    """
    root = ET.fromstring(text)
    suites = list(root.iter("testsuite"))
    totals = {
        key: sum(_attr_int(suite, key) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    duration = sum(_attr_float(suite, "time") for suite in suites)
    not_passed = totals["failures"] + totals["errors"] + totals["skipped"]
    return {
        "tests": totals["tests"],
        "failed": totals["failures"] + totals["errors"],
        "skipped": totals["skipped"],
        # Clamped at zero: a report whose parts exceed its total is malformed, but showing a
        # negative pass count would be a worse answer than showing none of them.
        "passed": max(totals["tests"] - not_passed, 0),
        "duration_sec": round(duration, 3),
        "failed_names": _failed_names(root),
    }


def _failed_names(root: ET.Element) -> list[str]:
    """
    Names of cases carrying a `<failure>` or `<error>` child, in document order.

    Document order rather than sorted: it matches the order the runner reported them, which
    is the order a developer will scroll their own terminal to find.
    """
    names = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        names.append(_case_name(case))
    return names


def _case_name(case: ET.Element) -> str:
    name = case.get("name") or "?"
    classname = case.get("classname")
    return f"{classname}::{name}" if classname else name


def merge_junit(reports: list[dict]) -> dict:
    """
    Combine several JUnit documents into one set of totals.

    Gradle and Maven write one file per test class, so a configured `junit` path is often a
    directory. Aggregation is a plain sum in the caller's (sorted) file order, which makes
    the result depend on the reports rather than on directory-iteration order.
    """
    counts = {key: sum(report[key] for report in reports) for key in _SUM_KEYS}
    return {
        **counts,
        "duration_sec": round(sum(report["duration_sec"] for report in reports), 3),
        "failed_names": [name for report in reports for name in report["failed_names"]],
    }


#: Counts that aggregate by plain addition. `duration_sec` and `failed_names` do not — one
#: rounds, the other concatenates — so they are combined separately rather than swept in here.
_SUM_KEYS = ("tests", "passed", "failed", "skipped")


def parse_cobertura(text: str) -> dict | None:
    """
    Coverage from a Cobertura-compatible document: how much, and how much of what.

    Every figure is a summary attribute on the root element, which coverage.py, JaCoCo's
    Cobertura exporter and Istanbul all write -- so the size of the measured code comes from
    the coverage tool itself rather than from Kiln counting lines in a language it would have
    to recognise first.

    `lines-valid` is the technology-agnostic answer to "how big is this": *executable
    statements measured*. Blank lines, comments, imports and braces are already excluded by
    the tool that knows the language, which is why this is worth more than a line count Kiln
    could produce on its own -- and why it needs no per-ecosystem branch to read.

    Returns None when `line-rate` is absent, so an unrecognised dialect reads as *unknown
    coverage* rather than as zero coverage.
    """
    root = ET.fromstring(text)
    if root.get("line-rate") is None:
        return None
    return {
        "line_percent": _rate_percent(root, "line-rate"),
        "lines_covered": _attr_int(root, "lines-covered"),
        "lines_valid": _attr_int(root, "lines-valid"),
        **_branch_coverage(root),
    }


#: Branch figures, absent together. Branch coverage is opt-in in most tools, so all three read
#: None when it was not measured rather than being reported as zeroes.
_BRANCH_KEYS = ("branch_percent", "branches_covered", "branches_valid")


def _branch_coverage(root: ET.Element) -> dict:
    """
    Branch figures, or None for each when no branches were measured.

    `branches-valid="0"` is what coverage.py writes without `--cov-branch`, and what JaCoCo
    writes for code containing no branches at all. It arrives alongside `branch-rate="0"`,
    which would render as "0% branch" and read as a catastrophe rather than as a measurement
    nobody switched on. Nothing to cover is not the same fact as nothing covered.
    """
    if _attr_int(root, "branches-valid") <= 0:
        return dict.fromkeys(_BRANCH_KEYS)
    return {
        "branch_percent": _rate_percent(root, "branch-rate"),
        "branches_covered": _attr_int(root, "branches-covered"),
        "branches_valid": _attr_int(root, "branches-valid"),
    }


def _rate_percent(element: ET.Element, name: str) -> float:
    """Cobertura rates are 0..1 ratios; the panel talks in percent."""
    return round(_attr_float(element, name) * 100, 2)


#: SARIF's own default when a result carries no level and its rule declares none
#: (§3.27.10). Not "error": a tool that omits the field is not thereby reporting failures.
SARIF_DEFAULT_LEVEL = "warning"

#: Levels worth counting. SARIF also defines "none", meaning the result carries no severity
#: at all -- a metric or a suppression note, not a finding, so it is not a violation.
SARIF_LEVELS = ("error", "warning", "note")


def parse_sarif(text: str) -> dict:
    """
    Violation counts and tool names from one SARIF 2.1.0 document.

    Read by *format*, never by tool. The analyser identifies itself in
    `runs[].tool.driver.name`, so ruff, ESLint, PMD, SpotBugs, Checkstyle and CodeQL all
    parse here with no branch per language -- which is the same property that made JUnit and
    Cobertura the right boundaries for the other two metrics.

    A document may carry several runs (one per tool, which is how a Java build merges PMD and
    SpotBugs into one file), so every run is walked and the counts summed.
    """
    runs = _as_list(json.loads(text).get("runs"))
    levels: Counter[str] = Counter()
    for run in runs:
        levels.update(_run_levels(run))
    return _lint_totals(levels, _tool_names(runs))


def _driver(run: dict) -> dict:
    return (run.get("tool") or {}).get("driver") or {}


def _run_levels(run: dict) -> Counter[str]:
    """Severity tally for one run, with each result's level resolved per the spec."""
    defaults = _rule_default_levels(_driver(run))
    return Counter(
        _result_level(result, defaults)
        for result in _as_list(run.get("results"))
        if _is_finding(result)
    )


def _tool_names(runs: list) -> list[str]:
    """Analyser names in document order, de-duplicated. Whatever the file says it ran."""
    names: list[str] = []
    for run in runs:
        name = _driver(run).get("name")
        if name and str(name) not in names:
            names.append(str(name))
    return names


def _as_list(value: object) -> list:
    """SARIF arrays are optional everywhere; a missing one is empty, not an error."""
    return value if isinstance(value, list) else []


def _is_finding(result: dict) -> bool:
    """
    Whether a result is a violation rather than an observation.

    `kind` defaults to "fail" when absent (§3.27.9). CodeQL and some Java analysers emit
    `kind: "pass"` or `"informational"` rows in the same file, and counting those as
    problems would report a clean run as dozens of findings.
    """
    return str(result.get("kind", "fail")) == "fail"


def _rule_default_levels(driver: dict) -> dict[str, str]:
    """Rule id -> its `defaultConfiguration.level`, for results that omit their own."""
    levels = {}
    for rule in _as_list(driver.get("rules")):
        level = (rule.get("defaultConfiguration") or {}).get("level")
        if rule.get("id") and level:
            levels[str(rule["id"])] = str(level)
    return levels


def _result_level(result: dict, defaults: dict[str, str]) -> str:
    """The spec's resolution order: the result's own level, then its rule's, then warning."""
    level = result.get("level") or defaults.get(str(result.get("ruleId") or ""))
    return str(level or SARIF_DEFAULT_LEVEL)


def _lint_totals(levels: Counter[str], tools: list[str]) -> dict:
    counts = {name: levels.get(name, 0) for name in SARIF_LEVELS}
    return {**counts, "total": sum(counts.values()), "tools": tools}


def merge_sarif(reports: list[dict]) -> dict:
    """
    Combine several SARIF documents, as a project running two analysers produces.

    Tool names are concatenated in file order and de-duplicated, so the panel can say which
    analysers contributed without depending on directory-iteration order.
    """
    counts = {name: sum(report[name] for report in reports) for name in SARIF_LEVELS}
    tools: list[str] = []
    for report in reports:
        tools.extend(tool for tool in report["tools"] if tool not in tools)
    return {**counts, "total": sum(counts.values()), "tools": tools}


def unavailable(reason: str, *, config: TestMetricsConfig | None = None) -> dict:
    """
    The empty/error state: everything unknown, and a sentence saying why.

    Carries exactly the keys `build_payload` does. A consumer that has to ask which shape it
    received before reading a field is a consumer that will eventually forget to.
    """
    return {
        "status": STATUS_UNAVAILABLE,
        "configured": bool(config and config.configured),
        "source": config.framework if config else "",
        "error": reason,
        "coverage": None,
        "updated_at": None,
        "stale": False,
        "max_age_minutes": config.max_age_minutes if config else DEFAULT_MAX_AGE_MINUTES,
        "failed_names": [],
        "lint": None,
        **dict.fromkeys(_COUNT_KEYS),
    }


def build_payload(
    *,
    config: TestMetricsConfig,
    junit: dict | None,
    coverage: dict | None,
    lint: dict | None,
    freshness: list[datetime],
    now: datetime,
) -> dict:
    """
    The `/api/test-metrics` document.

    Staleness outranks the result: a report older than the configured window describes code
    that has since moved on, and calling that "passed" is precisely the reassurance an
    operator should not be given. The counts stay in the payload either way, so the panel can
    show what the stale run said.

    `freshness` carries one timestamp per configured report -- the newest file *within* that
    report, since a per-class JUnit directory is one report in many files. The displayed age
    is the newest of them ("when did anything last refresh"), but staleness is judged on the
    *oldest*, so a coverage file rewritten a minute ago cannot disguise a JUnit report from
    yesterday.
    """
    stale = any(
        is_stale(stamp, now=now, max_age_minutes=config.max_age_minutes) for stamp in freshness
    )
    updated_at = max(freshness) if freshness else None
    payload = {
        "status": _status(junit, stale=stale),
        "configured": config.configured,
        "source": config.framework,
        "error": "",
        "coverage": coverage,
        "lint": lint,
        "updated_at": updated_at.isoformat(timespec="seconds") if updated_at else None,
        "stale": stale,
        "max_age_minutes": config.max_age_minutes,
    }
    if junit is None:
        payload.update(dict.fromkeys(_COUNT_KEYS))
        payload["failed_names"] = []
        return payload
    payload.update({key: junit[key] for key in _COUNT_KEYS})
    payload["failed_names"] = junit["failed_names"][:FAILED_NAME_LIMIT]
    return payload


#: The metrics a JUnit report supplies. Absent one, every entry reads None (unknown) rather
#: than zero -- "no report" and "a suite with no tests" are different facts.
_COUNT_KEYS = ("tests", "passed", "failed", "skipped", "duration_sec")


def _status(junit: dict | None, *, stale: bool) -> str:
    if stale:
        return STATUS_STALE
    if junit is None:
        # Coverage without a JUnit report is a real configuration: there is a number to show
        # but no pass/fail verdict to give, so the headline stays unavailable.
        return STATUS_UNAVAILABLE
    return STATUS_FAILED if junit["failed"] else STATUS_PASSED


def is_stale(updated_at: datetime | None, *, now: datetime, max_age_minutes: int) -> bool:
    """
    Whether a report is too old to describe the current tree.

    A future timestamp is never stale. Clock skew between a container that wrote the report
    and the host reading it is common, and the failure mode of guessing wrong here -- calling
    a report that was written seconds ago stale -- is more confusing than trusting it.
    """
    if updated_at is None:
        return False
    return now - updated_at > timedelta(minutes=max_age_minutes)
