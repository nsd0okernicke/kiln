"""Combine coverage.py JSON and Radon JSON into per-function CRAP reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def crap_score(complexity: int, coverage: float) -> float:
    """CRAP(m), with coverage expressed from 0.0 to 1.0."""
    coverage = min(1.0, max(0.0, coverage))
    return complexity**2 * (1 - coverage) ** 3 + complexity


def _coverage_file(files: dict[str, Any], radon_path: str) -> dict[str, Any]:
    wanted = radon_path.replace("\\", "/")
    for name, payload in files.items():
        normalized = name.replace("\\", "/")
        if normalized == wanted or normalized.endswith("/" + wanted):
            return payload
    return {}


def build_report(coverage: dict[str, Any], complexity: dict[str, Any]) -> list[dict[str, Any]]:
    """Return functions sorted by descending CRAP score."""
    rows: list[dict[str, Any]] = []
    files = coverage.get("files", {})
    for filename, blocks in complexity.items():
        measured = _coverage_file(files, filename)
        executed = set(measured.get("executed_lines", []))
        missing = set(measured.get("missing_lines", []))
        for block in blocks:
            if block.get("type") not in {"function", "method"}:
                continue
            start = int(block["lineno"])
            end = int(block.get("endline", start))
            relevant = {line for line in executed | missing if start <= line <= end}
            covered = len(relevant & executed)
            ratio = covered / len(relevant) if relevant else 1.0
            score = crap_score(int(block["complexity"]), ratio)
            rows.append(
                {
                    "file": filename,
                    "name": block["name"],
                    "line": start,
                    "end_line": end,
                    "complexity": int(block["complexity"]),
                    "coverage": round(ratio, 4),
                    "crap": round(score, 2),
                }
            )
    return sorted(rows, key=lambda row: (-row["crap"], row["file"], row["line"]))


def markdown(rows: list[dict[str, Any]], threshold: float = 6.0) -> str:
    above = sum(row["crap"] > threshold for row in rows)
    lines = [
        "# CRAP hotspots",
        "",
        f"Functions: {len(rows)} · above {threshold:g}: {above}",
        "",
        "| CRAP | Complexity | Coverage | Function |",
        "|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['crap']:.2f} | {row['complexity']} | {row['coverage']:.1%} | "
            f"`{row['file']}:{row['line']} {row['name']}` |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--complexity", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=6.0)
    args = parser.parse_args(argv)
    rows = build_report(
        json.loads(args.coverage.read_text(encoding="utf-8")),
        json.loads(args.complexity.read_text(encoding="utf-8")),
    )
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(
            {
                "formula": "complexity^2 * (1 - coverage)^3 + complexity",
                "threshold": args.threshold,
                "above_threshold": sum(row["crap"] > args.threshold for row in rows),
                "functions": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    args.markdown.write_text(markdown(rows, args.threshold), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
