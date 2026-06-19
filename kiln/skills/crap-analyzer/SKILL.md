---
name: crap-analyzer
description: Identify complex, under-tested code using CRAP analysis.
---

# CRAP Analyzer Skill

You are an expert in analyzing code risk, complexity, and maintainability.

## Purpose

- Evaluate design risk using complexity and coverage insights.
- Identify code that is hard to understand or risky to change.
- CRAP = Change Risk Anti-Pattern: a combined metric of complexity and test coverage.

## CRAP Formula

```
CRAP = Complexity² × (1 - Coverage) + Complexity
```

Where:
- **Complexity**: cyclomatic complexity (number of decision branches)
- **Coverage**: fraction of code executed by tests (0.0 to 1.0)

**Interpretation**:
- Low complexity (< 3) and high coverage (> 90%) → CRAP close to 0 (good)
- High complexity (> 5) and low coverage (< 60%) → CRAP >> 6 (bad)
- High complexity (> 5) but high coverage (> 90%) → CRAP = complexity/2 (acceptable if fully tested)

**Acceptable threshold** (per `refactorer.md`): CRAP ≤ 6 per function (different from JVM `crap-analyzer` threshold of 30, which uses PIT's CRAP formula).

## Instructions

1. Run complexity and coverage tools for your project language.
   - **Python**: `radon cc -s -n C src/` (complexity) + `coverage report` (coverage %)
   - **Java/Kotlin**: `./gradlew jacocoTestReport` (complexity in JaCoCo) + parse XML for coverage
   - **Go**: `go build` with complexity tools + `go test -cover`

2. For each function flagged with complexity ≥ 3:
   - Extract its test coverage from coverage report
   - Compute CRAP = complexity² × (1 - coverage) + complexity
   - Compare to threshold (≤ 6 for acceptable, > 6 for needs action)

3. Sort functions by CRAP score descending. Flag all with CRAP > 6.

4. Always list the top-5 highest-CRAP functions regardless of threshold.

### Self-Critique

After producing the report, review in one short paragraph:
- Are CRAP calculations plausible? Spot-check one function by hand.
- Is the top-risk recommendation concrete (extract helper, add tests, etc.) rather than vague?
- Did you consider whether adding tests is cheaper than reducing complexity?

## Output Format

### CRAP Score Report

| Function | Complexity | Coverage % | CRAP Score | Recommendation |
|---|---|---|---|---|
| `process_order()` | 6 | 85% | 8.4 | Extract discount logic to reduce complexity to 4 |
| `validate_order()` | 4 | 100% | 2.0 | Good; no action needed |

**Top risk**: function with highest CRAP and the single most impactful fix.

### JSON Status Block

```json
{
  "skill": "crap-analyzer",
  "high_risk_methods": 3,
  "max_crap_score": 8.4,
  "threshold": 6,
  "critique": "<one-line self-critique>",
  "status": "passed | failed"
}
```

## Threshold Note

⚠️ **Formula difference**: JVM's PIT uses a different CRAP formula (threshold 30) than Python's radon (threshold ≤ 6 complexity score). Both formulas identify risk, but scales differ. Use your project's declared threshold; if unsure, default to the formula-specific threshold documented in `constitution/engineering.md` or `refactorer.md`.

## If Tools Are Unavailable

- **Tool not installed**: report which tool (e.g., "radon") and install command.
- **Report file missing**: capture build error output; do not parse non-existent files.
- **In all error cases**: emit JSON with `"status": "error"` and an `"error"` field.

