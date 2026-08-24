"""Summarize Cosmic Ray session databases without running mutation tests."""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
from pathlib import Path


def _annotation_spans(module_path: Path) -> list[tuple[int, int, int, int]]:
    """Return source spans occupied by annotations in a Python module."""
    if not module_path.is_file():
        return []
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.extend(
                annotation
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if (annotation := argument.annotation) is not None
            )
            if node.args.vararg and node.args.vararg.annotation:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation:
                annotations.append(node.args.kwarg.annotation)
            if node.returns:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return [
        (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        for node in annotations
        if node.end_lineno is not None and node.end_col_offset is not None
    ]


def _inside_span(row: int, column: int, span: tuple[int, int, int, int]) -> bool:
    start_row, start_column, end_row, end_column = span
    return (row, column) >= (start_row, start_column) and (row, column) < (
        end_row,
        end_column,
    )


def _annotation_outcomes(conn: sqlite3.Connection) -> dict[str, int]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"mutation_specs", "work_results"}.issubset(tables):
        return {}
    spans_by_module: dict[str, list[tuple[int, int, int, int]]] = {}
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT module_path, start_pos_row, start_pos_col, test_outcome "
        "FROM mutation_specs JOIN work_results USING(job_id) "
        "WHERE test_outcome IS NOT NULL"
    )
    for module, row, column, outcome in rows:
        spans = spans_by_module.setdefault(module, _annotation_spans(Path(module)))
        if any(_inside_span(row, column, span) for span in spans):
            key = str(outcome).lower()
            counts[key] = counts.get(key, 0) + 1
    return counts


def summarize(path: Path) -> dict[str, object]:
    """Count outcome-like values in a Cosmic Ray SQLite session."""
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        for table in tables:
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                if "outcome" not in column.lower() and "status" not in column.lower():
                    continue
                rows = conn.execute(
                    f'SELECT "{column}", COUNT(*) FROM "{table}" GROUP BY "{column}"'
                )
                for value, count in rows:
                    if value is not None:
                        key = str(value).lower().replace(" ", "_")
                        counts[key] = counts.get(key, 0) + int(count)
        annotation_outcomes = _annotation_outcomes(conn)
    killed = sum(value for key, value in counts.items() if "killed" in key)
    survived = sum(value for key, value in counts.items() if "surviv" in key)
    denominator = killed + survived
    annotation_killed = annotation_outcomes.get("killed", 0)
    annotation_survived = annotation_outcomes.get("survived", 0)
    behavioral_killed = killed - annotation_killed
    behavioral_survived = survived - annotation_survived
    behavioral_denominator = behavioral_killed + behavioral_survived
    try:
        database = str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        database = str(path)
    return {
        "database": database,
        "outcomes": counts,
        "killed": killed,
        "survived": survived,
        "mutation_score": killed / denominator if denominator else None,
        "score_formula": "killed / (killed + survived)",
        "annotation_outcomes": annotation_outcomes,
        "behavioral_killed": behavioral_killed,
        "behavioral_survived": behavioral_survived,
        "behavioral_mutation_score": (
            behavioral_killed / behavioral_denominator if behavioral_denominator else None
        ),
        "behavioral_score_formula": (
            "(killed - annotation killed) / "
            "((killed + survived) - (annotation killed + annotation survived))"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    payload = {path.stem: summarize(path) for path in args.sessions if path.is_file()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
