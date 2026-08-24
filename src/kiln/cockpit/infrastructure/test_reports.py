"""
Reading test-report files off disk for the cockpit's Test health panel (issue #27).

The impure half of `cockpit.application.test_metrics`, split for the same reason
`gather_state` is split from `build_state`: the parsers stay testable on document text, and
every way the filesystem can disappoint -- absent file, unreadable file, empty directory,
XML that is not XML -- is caught in one place and turned into an explanatory payload.

Nothing here runs a test command. The cockpit polls every two seconds; a monitoring surface
that shelled out to a build tool on that loop would be a fault, not a feature.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from kiln.cockpit.application import test_metrics

log = logging.getLogger(__name__)

#: Where a project describes its reports, relative to the project root.
#:
#: Under `.kiln/` with the rest of Kiln's per-project files rather than as a `kiln.*` file at
#: the project root. Two consequences worth stating plainly, because they are not obvious from
#: the path: `.kiln/` is in `REQUIRED_GITIGNORE_ENTRIES` and the launcher tops that rule back
#: up on every start, so this file can never be committed, shared through a clone, or survive
#: a teardown -- it is written by hand and re-written by hand. The empty-state message names
#: the full path for exactly that reason.
CONFIG_RELATIVE_PATH = Path(".kiln") / "test-metrics.json"

#: The path as a string, for messages and `--help`. One spelling, one source.
CONFIG_DISPLAY_PATH = CONFIG_RELATIVE_PATH.as_posix()


def config_path(project_root: Path) -> Path:
    """Where to look for this project's report configuration."""
    return project_root / CONFIG_RELATIVE_PATH


class ReportError(Exception):
    """
    A report exists but cannot be read as the format its configuration claims.

    Raised by the readers rather than handled there so `collect` keeps one exit for "the
    report is broken", while each reader still supplies the sentence naming *which* report --
    which is the part an operator needs to fix the path.
    """


#: Read caps. A report is a summary; anything this large is a runaway or a wrong path, and
#: the cockpit should say so rather than pull it into memory on a two-second loop.
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_REPORT_FILES = 200


def load_config(path: Path) -> test_metrics.TestMetricsConfig | None:
    """
    Read the project's report configuration. None means the file is not there.

    Present-but-broken raises `ReportError` instead of reading as absent. Those are different
    facts and the panel must not conflate them: a typo in this file used to render as "no
    config, create one", which sends an operator to write a file that already exists.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError(f"{CONFIG_DISPLAY_PATH} is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise ReportError(f"{CONFIG_DISPLAY_PATH} must contain a JSON object")
    return test_metrics.config_from_mapping(payload)


#: Extensions a directory is scanned for, per report kind. XML for the two report formats
#: that are XML; SARIF is JSON and is written as `.sarif` by ruff and CodeQL but as `.json`
#: by several Java analysers, so both are accepted rather than picking a house style.
XML_SUFFIXES = (".xml",)
SARIF_SUFFIXES = (".sarif", ".json")


def report_files(
    root: Path, configured: str, suffixes: tuple[str, ...] = XML_SUFFIXES
) -> list[Path]:
    """
    Resolve a configured report path to the files to read, sorted by name.

    A directory yields its matching children sorted by name -- Gradle and Maven write one file
    per test class, and sorting by name rather than by mtime keeps a re-run that touches only
    some files from reordering the aggregate.

    An explicitly configured *file* is read whatever it is called: the suffixes filter only
    decides what a directory scan picks up, so a project whose reporter writes `results.txt`
    is not second-guessed.
    """
    if not configured:
        return []
    target = Path(configured)
    target = target if target.is_absolute() else root / target
    if target.is_dir():
        return _scan(target, suffixes)
    return [target] if target.is_file() else []


def _scan(directory: Path, suffixes: tuple[str, ...]) -> list[Path]:
    found = (path for path in directory.iterdir() if path.suffix.lower() in suffixes)
    return sorted(found)[:MAX_REPORT_FILES]


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            log.warning("ignoring oversized report %s", path)
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        log.warning("unreadable report %s: %s", path, error)
        return None


def _newest_mtime(paths: list[Path]) -> datetime | None:
    """
    Report age is the *newest* file's mtime.

    With one file per class, the oldest says when the run started and the newest when it
    finished; the finish is what "updated 3m ago" should mean.
    """
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(stamps)) if stamps else None


def collect(
    config: test_metrics.TestMetricsConfig, *, root: Path, now: datetime | None = None
) -> dict:
    """
    Read whatever is configured and hand back the `/api/test-metrics` document.

    Never raises. Every failure below becomes a payload with an `error` sentence, because
    this is a monitoring surface: a broken report must show as broken and must not take the
    endpoint -- or the swarm -- down with it.
    """
    now = now or datetime.now()
    if not config.configured:
        return test_metrics.unavailable("no test metrics configured", config=config)

    junit_paths = report_files(root, config.junit)
    if config.junit and not junit_paths:
        return test_metrics.unavailable(f"no report found at {config.junit}", config=config)

    coverage_paths = report_files(root, config.coverage)
    lint_paths = report_files(root, config.lint, SARIF_SUFFIXES)
    try:
        junit = _junit_totals(junit_paths)
        coverage = _coverage(coverage_paths)
        lint = _lint_totals(lint_paths)
    except ReportError as error:
        return test_metrics.unavailable(str(error), config=config)

    return test_metrics.build_payload(
        config=config,
        junit=junit,
        coverage=coverage,
        lint=lint,
        freshness=_freshness(junit_paths, coverage_paths, lint_paths),
        now=now,
    )


def _freshness(*groups: list[Path]) -> list[datetime]:
    """
    One timestamp per configured report: the newest file *within* it.

    Newest within a group because a per-class JUnit directory is one report in many files,
    and a suite re-run that leaves an orphaned file behind must not read as stale. Kept as a
    list rather than reduced here so `build_payload` can use the newest for the displayed age
    and the oldest for the staleness verdict.
    """
    stamps = [_newest_mtime(group) for group in groups if group]
    return [stamp for stamp in stamps if stamp is not None]


def _junit_totals(paths: list[Path]) -> dict | None:
    """None when nothing was configured or nothing could be read -- not an empty result."""
    try:
        parsed = [test_metrics.parse_junit(text) for path in paths if (text := _read(path))]
    except ET.ParseError as error:
        raise ReportError(f"malformed JUnit report: {error}") from error
    return test_metrics.merge_junit(parsed) if parsed else None


def _coverage(paths: list[Path]) -> dict | None:
    """
    Coverage figures from the first configured document, or None when there is none.

    First rather than merged, unlike JUnit and SARIF: coverage totals are ratios over a body
    of code, and adding two of them together produces a number that is not coverage of
    anything. A project that writes several coverage files should merge them with its own
    coverage tool, which knows which lines the two runs share.
    """
    if not paths:
        return None
    text = _read(paths[0])
    if not text:
        return None
    try:
        return test_metrics.parse_cobertura(text)
    except ET.ParseError as error:
        raise ReportError(f"malformed coverage report: {error}") from error


def _lint_totals(paths: list[Path]) -> dict | None:
    """
    Violation counts from every configured SARIF document, or None when none is configured.

    A directory is read whole, so a project running two analysers into one folder -- PMD and
    SpotBugs, say -- reports one combined figure without naming either tool in this code.
    """
    try:
        parsed = [test_metrics.parse_sarif(text) for path in paths if (text := _read(path))]
    except (json.JSONDecodeError, AttributeError, TypeError) as error:
        raise ReportError(f"malformed SARIF report: {error}") from error
    return test_metrics.merge_sarif(parsed) if parsed else None
