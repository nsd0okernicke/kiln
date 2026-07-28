---
name: tdd-green
description: Implement the smallest change needed to make the failing test pass.
---

# TDD Green Phase Skill

You are an expert at implementing the minimal code required to satisfy a failing test.

## Purpose
- Make the previously failing test pass with the least amount of code.
- Avoid unnecessary features, abstractions, or complexity.

## Conventions
- Keep implementation focused on the current failing test only.
- Preserve clean architecture: domain logic in `domain/`, use cases in `application/`, adapters in `infrastructure/`.
- Use language and test conventions from the selected config.

## Output Format

Structure your output in this order:

1. **Implementation code** — the minimal production code in a fenced code block.
2. **Self-critique** — review the implementation against Clean Architecture rules. One short paragraph: does anything leak across boundaries? Is domain logic in the right layer?
3. **Status block** — a fenced JSON block at the end:

```json
{
  "phase": "green",
  "file": "<path to implementation file>",
  "test": "<test that now passes>",
  "status": "passing",
  "lines_added": 0,
  "critique": "<one-line summary of self-critique>"
}
```

## Example

**Input**: Failing test `test_bestellung_abgelehnt_wenn_lager_leer` — `bestellung_aufgeben` does not exist.

**Implementation output**:

```python
# domain/bestellung.py
from dataclasses import dataclass
from domain.lager import Lager

@dataclass
class BestellungErgebnis:
    abgelehnt: bool
    grund: str = ""

def bestellung_aufgeben(artikel: str, menge: int, lager: Lager) -> BestellungErgebnis:
    if lager.bestand < menge:
        return BestellungErgebnis(abgelehnt=True, grund="Lagerbestand unzureichend")
    return BestellungErgebnis(abgelehnt=False)
```

**Self-critique**: Domain logic is correctly placed in `domain/`. No infrastructure dependencies. `BestellungErgebnis` is a pure value object with no side effects. Clean Architecture rules satisfied — nothing to flag.

```json
{
  "phase": "green",
  "file": "domain/bestellung.py",
  "test": "test_bestellung_abgelehnt_wenn_lager_leer",
  "status": "passing",
  "lines_added": 11,
  "critique": "Clean — pure domain logic, no infrastructure leakage"
}
```

## Instructions
- Refer to the selected config's `copilot-instructions.md` and `MEMORY.md` for stack-specific conventions.
- Do not over-engineer; keep changes minimal.
- Always write the self-critique before writing the status block.

