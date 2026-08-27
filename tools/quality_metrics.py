"""Cross-platform observe-first quality report driver for Kiln itself."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
PRODUCTION = ROOT / "src"
TOOLS = ("pytest", "pytest-cov", "coverage", "ruff", "radon", "pyright", "cosmic-ray", "bandit")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run(name: str, command: list[str], output: Path) -> int:
    """Run one report command, preserving combined human-readable output."""
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        rendered = result.stdout
        code = result.returncode
    except OSError as exc:
        rendered, code = f"could not run {name}: {exc}\n", 127
    _write(output, rendered)
    print(f"{name}: {'ok' if code == 0 else f'exit {code}'} -> {output.relative_to(ROOT)}")
    return code


def metadata(commands: dict[str, list[str]], results: dict[str, int]) -> None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout.strip()
        )
    except OSError:
        sha, dirty = "", None
    versions = {}
    for tool in TOOLS:
        try:
            versions[tool] = version(tool)
        except PackageNotFoundError:
            versions[tool] = None
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": sha,
        "worktree_dirty": dirty,
        "python": sys.version,
        "platform": platform.platform(),
        "tools": versions,
        "commands": commands,
        "results": results,
    }
    _write(REPORTS / "metadata.json", json.dumps(payload, indent=2) + "\n")


def duplication_report() -> None:
    """Report identical normalized function bodies; a signal, never a gate."""
    groups: dict[str, list[dict[str, object]]] = {}
    for path in PRODUCTION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lines = (node.end_lineno or node.lineno) - node.lineno + 1
            if lines < 8:
                continue
            normalized = ast.dump(
                ast.Module(body=node.body, type_ignores=[]), include_attributes=False
            )
            digest = hashlib.sha256(normalized.encode()).hexdigest()
            groups.setdefault(digest, []).append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "function": node.name,
                    "line": node.lineno,
                    "lines": lines,
                }
            )
    duplicates = [items for items in groups.values() if len(items) > 1]
    _write(
        REPORTS / "duplication" / "duplication.json",
        json.dumps(
            {
                "minimum_function_lines": 8,
                "duplicate_groups": duplicates,
            },
            indent=2,
        )
        + "\n",
    )


def summary() -> None:
    """Create a concise Markdown summary for humans and GitHub Actions."""
    coverage = json.loads((REPORTS / "coverage" / "coverage.json").read_text(encoding="utf-8"))
    junit = ET.parse(REPORTS / "tests" / "junit.xml").getroot()
    suite = junit.find("testsuite") if junit.tag == "testsuites" else junit
    crap = json.loads((REPORTS / "complexity" / "crap.json").read_text(encoding="utf-8"))
    types = json.loads((REPORTS / "types" / "typecheck.json").read_text(encoding="utf-8"))
    totals = coverage["totals"]
    scheduler_files = [
        payload
        for name, payload in coverage["files"].items()
        if "/scheduler/" in name.replace("\\", "/") and "/adapters/" not in name.replace("\\", "/")
    ]
    scheduler = {
        key: sum(item["summary"].get(key, 0) for item in scheduler_files)
        for key in ("covered_lines", "num_statements", "covered_branches", "num_branches")
    }
    scheduler["statement_percent"] = (
        100 * scheduler["covered_lines"] / scheduler["num_statements"]
        if scheduler["num_statements"]
        else None
    )
    scheduler["branch_percent"] = (
        100 * scheduler["covered_branches"] / scheduler["num_branches"]
        if scheduler["num_branches"]
        else None
    )
    _write(
        REPORTS / "coverage" / "scheduler-core.json",
        json.dumps(scheduler, indent=2) + "\n",
    )
    attrs = suite.attrib if suite is not None else {}
    text = (
        "# Kiln quality metrics\n\n"
        f"- Tests: {attrs.get('tests', '?')} total, {attrs.get('failures', '?')} failed, "
        f"{attrs.get('skipped', '?')} skipped in {attrs.get('time', '?')}s\n"
        f"- Statement coverage: {totals['percent_statements_covered']:.2f}%\n"
        f"- Branch coverage: {totals['percent_branches_covered']:.2f}%\n"
        f"- Scheduler core statement/branch: {scheduler['statement_percent']:.2f}% / "
        f"{scheduler['branch_percent']:.2f}%\n"
        f"- CRAP hotspots above {crap['threshold']:g}: {crap['above_threshold']}\n"
        f"- Pyright errors: {types.get('summary', {}).get('errorCount', '?')}\n"
    )
    _write(REPORTS / "summary.md", text)
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("fast", "deterministic"), default="deterministic")
    parser.add_argument("--observe", action="store_true", help="record failures without failing")
    args = parser.parse_args(argv)
    py = sys.executable
    commands: dict[str, list[str]] = {
        "tests": [
            py,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--junitxml=reports/tests/junit.xml",
            "--durations=25",
            "--cov=src",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=xml:reports/coverage/coverage.xml",
            "--cov-report=json:reports/coverage/coverage.json",
            "--cov-report=html:reports/coverage/html",
        ],
        "ruff": [py, "-m", "ruff", "check", "src", "tests", "tools"],
        "ruff_json": [py, "-m", "ruff", "check", "--output-format=json", "src", "tests", "tools"],
        "bandit": [py, "-m", "bandit", "-r", "src/kiln", "-c", "pyproject.toml"],
        "vulture": [py, "-m", "vulture", "src/kiln", "--min-confidence", "80"],
        "format": [py, "-m", "ruff", "format", "--check", "src", "tests", "tools"],
        "radon_cc_json": [py, "-m", "radon", "cc", "-j", "src"],
        "radon_cc_text": [py, "-m", "radon", "cc", "-s", "-a", "src"],
        "radon_mi": [py, "-m", "radon", "mi", "-j", "src"],
        "radon_raw": [py, "-m", "radon", "raw", "-j", "src"],
        "types": [py, "-m", "pyright", "--outputjson", "src/kiln"],
    }
    outputs = {
        "tests": REPORTS / "tests" / "slowest.txt",
        "bandit": REPORTS / "lint" / "bandit.txt",
        "vulture": REPORTS / "lint" / "vulture.txt",
        "ruff": REPORTS / "lint" / "ruff.txt",
        "ruff_json": REPORTS / "lint" / "ruff.json",
        "format": REPORTS / "lint" / "format.txt",
        "radon_cc_json": REPORTS / "complexity" / "radon-cc.json",
        "radon_cc_text": REPORTS / "complexity" / "radon-cc.txt",
        "radon_mi": REPORTS / "complexity" / "radon-mi.json",
        "radon_raw": REPORTS / "complexity" / "radon-raw.json",
        "types": REPORTS / "types" / "typecheck.json",
    }
    selected = (
        ["bandit", "vulture", "ruff", "format", "types"] if args.tier == "fast" else list(commands)
    )
    results = {name: run(name, commands[name], outputs[name]) for name in selected}
    coverage_json = REPORTS / "coverage" / "coverage.json"
    if (
        args.tier == "deterministic"
        and results.get("radon_cc_json") == 0
        and coverage_json.is_file()
    ):
        run(
            "crap",
            [
                py,
                "-m",
                "tools.crap_report",
                "--coverage",
                "reports/coverage/coverage.json",
                "--complexity",
                "reports/complexity/radon-cc.json",
                "--json",
                "reports/complexity/crap.json",
                "--markdown",
                "reports/complexity/crap.md",
            ],
            REPORTS / "complexity" / "crap-driver.txt",
        )
        duplication_report()
        required = (
            REPORTS / "tests" / "junit.xml",
            REPORTS / "complexity" / "crap.json",
            REPORTS / "types" / "typecheck.json",
        )
        if all(path.is_file() for path in required):
            summary()
    metadata({name: command for name, command in commands.items() if name in selected}, results)
    hard = (
        {"tests", "ruff", "bandit", "vulture"}
        if args.tier == "deterministic"
        else {"bandit", "vulture", "ruff"}
    )
    failed = [name for name in hard if results.get(name, 0) != 0]
    return 0 if args.observe or not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
