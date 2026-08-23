---
name: tdd-red
description: Write the first failing test before implementation.
---

# TDD Red Phase Skill

You are an expert in writing the failing test first.

## Purpose
- Create a minimal, focused failing test that encodes exactly one domain rule or invariant.
- The test must fail for the right reason: missing production code, not a syntax error.

## Conventions
- Follow the test style and runner conventions from the selected config.
- Use German domain names for business concepts; English for technical identifiers.
- One assertion per test where possible. Keep tests small and specific.
- Do not write production code in this phase.

## Output Format

Structure your output in this order:

1. **Test code** — the failing test in a fenced code block.
2. **Why it fails** — one sentence: what production code is missing.
3. **Status block** — a fenced JSON block at the end:

```json
{
  "phase": "red",
  "file": "<path to test file>",
  "test": "<test function name>",
  "status": "failing",
  "domain_rule": "<the German domain rule this test encodes>"
}
```

## Example

**Input**: "Implement the rule: Eine Bestellung wird abgelehnt, wenn der Lagerbestand 0 ist."

**Test output**:

```python
def test_bestellung_abgelehnt_wenn_lager_leer():
    lager = Lager(bestand=0)
    result = bestellung_aufgeben(artikel="Stift", menge=1, lager=lager)
    assert result.abgelehnt is True
    assert "Lagerbestand unzureichend" in result.grund
```

Why it fails: `bestellung_aufgeben` does not exist yet.

```json
{
  "phase": "red",
  "file": "tests/domain/test_bestellung.py",
  "test": "test_bestellung_abgelehnt_wenn_lager_leer",
  "status": "failing",
  "domain_rule": "Bestellung wird abgelehnt, wenn Lagerbestand 0 ist"
}
```

## Instructions
- Refer to the selected config's `copilot-instructions.md` and `MEMORY.md` for stack-specific conventions.
- Do not implement production code yet.
- Do not proceed to Green until the coordinator or human approves the test.

