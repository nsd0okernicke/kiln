---
name: mutation-testing
description: Evaluate mutation testing and DRY analysis results; recommend improvements.
---

# Mutation Testing and DRY Analysis Skill

You are an expert at interpreting mutation testing and code duplication feedback.

## Purpose

- **Mutation Testing**: Analyze surviving mutants and identify weak assertions. Recommend stronger tests for risky code paths.
- **DRY (Don't Repeat Yourself)**: Identify duplicated code and recommend consolidation opportunities.

## Conventions

- Treat surviving mutants as hints for missing assertions or behavior checks.
- Treat duplicated code as a refactoring opportunity to reduce maintenance burden.
- Suggest minimal, targeted improvements.

## Mutation Testing Instructions

1. Run your project's mutation testing tool (see `constitution/engineering.md` or language-specific toolchain).
   - **Python (mutmut)**: `mutmut run --tests-dir tests` then `mutmut results --json`
   - **Java/Kotlin (PIT)**: `./gradlew pitest` then parse `build/reports/pitest/mutations.xml`
   - **Go (stryker)**: `stryker run --target <package>`

2. Extract mutation score and surviving mutant details from tool output.

3. Compare to threshold (typical: ≥ 70% killed; check project documentation).

4. Group surviving mutants by function/class. For each survivor:
   - Describe the mutation type (boundary change, return value swap, condition inversion, etc.)
   - Explain what assertion or test case would kill it
   - Recommend one concrete test improvement

## DRY Analysis Instructions

1. Run your project's duplication analysis tool.
   - **Python (radon)**: `radon mi -s src/` then check maintainability index
   - **Java/Kotlin (detekt/SonarQube)**: `./gradlew detekt` or SonarQube analysis
   - **Go (duplication)**: custom tool or grep-based pattern analysis

2. Identify duplicated code blocks (same logic repeated 3+ times).

3. For each duplication:
   - Show the duplicated pattern (2-3 lines of code)
   - List all locations where it appears
   - Recommend a shared helper function or utility class

## Output Format

### Mutation Testing Report

**Mutation score**: X% (killed Y / survived Z / total W)

| Function | Line | Mutation Type | Surviving Mutant | Missing Assertion |
|---|---|---|---|---|
| `validate_order()` | 42 | boundary change `>` → `>=` | rejects when `qty=0` instead of `qty < 0` | assert zero quantity rejected |

### DRY Analysis Report

**Duplication found**: N code blocks

| Pattern | Locations | Suggested Refactor |
|---|---|---|
| `if item.in_stock and item.price > 0:` | order.py:34, cart.py:18, checkout.py:52 | extract `is_purchasable(item)` helper |

### JSON Status Block

```json
{
  "skill": "mutation-testing",
  "mutation_score": 75,
  "survived": 5,
  "killed": 20,
  "duplications": 3,
  "status": "passed | failed"
}
```

## If Tools Are Unavailable

- **Tool not installed**: report which tool (e.g., "mutmut") and install command (e.g., `pip install mutmut`).
- **Report file missing**: capture build error output; do not parse non-existent files.
- **Mutation run times out**: reduce scope or increase timeout; note in the report.
- **In all error cases**: emit JSON with `"status": "error"` and an `"error"` field.

