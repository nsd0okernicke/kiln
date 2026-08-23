"""Summarize Cosmic Ray session databases without running mutation tests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def summarize(path: Path) -> dict[str, object]:
    """Count outcome-like values in a Cosmic Ray SQLite session."""
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
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
    killed = sum(value for key, value in counts.items() if "killed" in key)
    survived = sum(value for key, value in counts.items() if "surviv" in key)
    denominator = killed + survived
    return {
        "database": str(path), "outcomes": counts,
        "killed": killed, "survived": survived,
        "mutation_score": killed / denominator if denominator else None,
        "score_formula": "killed / (killed + survived)",
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
