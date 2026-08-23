from tools.crap_report import build_report, crap_score, markdown


def test_crap_formula_rewards_covered_complexity():
    assert crap_score(5, 1.0) == 5
    assert crap_score(5, 0.0) == 30


def test_report_combines_function_lines_and_sorts_hotspots():
    coverage = {
        "files": {
            "src/a.py": {
                "executed_lines": [1, 2, 10],
                "missing_lines": [3, 11, 12],
            }
        }
    }
    complexity = {
        "src/a.py": [
            {"type": "function", "name": "covered", "lineno": 1, "endline": 3, "complexity": 2},
            {"type": "method", "name": "risky", "lineno": 10, "endline": 12, "complexity": 5},
            {"type": "class", "name": "Ignored", "lineno": 1, "endline": 12, "complexity": 9},
        ]
    }

    rows = build_report(coverage, complexity)

    assert [row["name"] for row in rows] == ["risky", "covered"]
    assert rows[0]["coverage"] == 0.3333
    assert "above 6" in markdown(rows)
