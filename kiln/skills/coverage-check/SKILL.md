---
name: coverage-check
description: Check test coverage and identify missing behavior tests.
---

# Coverage Check Skill

You are an expert in test coverage analysis and gap identification.

## Purpose

- Identify behavior that is not covered by existing tests.
- Recommend targeted tests to improve confidence.

## Conventions

- Focus on business behavior and edge cases, not only line counts.
- Prefer coverage improvements in critical logic.

## Instructions

1. Run your project's coverage tool (see `constitution/engineering.md` or language-specific toolchain for exact command).
   - **Python (coverage.py)**: `coverage run -m pytest tests/` then `coverage report --skip-covered`
   - **Java/Kotlin (JaCoCo)**: `./gradlew jacocoTestReport` then parse `build/reports/jacoco/test/jacocoTestReport.xml`
   - **Go (cover)**: `go test -cover ./...` or `go test -coverprofile=coverage.out ./...`

2. Extract per-module coverage (line % and branch % if available) from tool output.

3. Compare to thresholds (typical: LINE ≥ 80%, BRANCH ≥ 75%; check project documentation).

4. List all modules below threshold, sorted by gap size (largest first).

5. For each under-covered module, identify uncovered functions/methods and recommend the highest-value test to add.

## Output Format

### Coverage Report

| Module / Class | Line % | Branch % | Status |
|---|---|---|---|
| `order_processor.py` | 85% | 62% | BRANCH BELOW |

**Overall**: line X%, branch Y%.

### Actionable Recommendations

For each under-covered module:
- List functions with 0% coverage or incomplete branch coverage
- Recommend one targeted test that would improve coverage the most
- Example: "Add test for `validate_order()` with invalid items to cover the rejection branch"

### JSON Status Block

```json
{
  "skill": "coverage-check",
  "line_coverage": 85.5,
  "branch_coverage": 62.0,
  "below_threshold": ["order_processor.py"],
  "status": "passed | failed"
}
```

## If Tools Are Unavailable

- **Tool not found**: report which tool is missing (e.g., "coverage not installed") and the install command (e.g., `pip install coverage`).
- **Report file missing**: capture and report build error output; do not attempt to parse non-existent files.
- **In all error cases**: emit the JSON status block with `"status": "error"` and an `"error"` field.

