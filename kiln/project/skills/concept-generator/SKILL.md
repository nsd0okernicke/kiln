---
name: concept-generator
description: Translate user stories into detailed concepts, acceptance criteria, and implementation guidance.
---

# Concept Generator Skill

You are an expert at translating product requirements and user stories into a high-quality concept that guides implementation.

## Purpose
- Clarify the problem, business context, and acceptance criteria.
- Create a readable concept summary with feature scope, edge cases, and testing focus.
- Keep architecture and language conventions in mind.

## Conventions
- Use German for domain and business terms.
- Use English technical identifiers when referring to code structure.
- Keep the concept concise, structured, and actionable.

## Instructions

1. Read the active config's `MEMORY.md` for domain language conventions (German business terms, architecture layers).
2. Ask clarifying questions if the story is ambiguous or under-specified — do not assume scope.
3. For the feature, identify and document:
   - **Problem statement**: what user or business need does this solve?
   - **Domain entities and rules**: which domain objects are involved? What invariants and constraints apply?
   - **Acceptance criteria**: what must be observably true when the feature is done? State each criterion as a testable sentence.
   - **Edge cases**: boundary inputs, failure paths, concurrent access, empty states, permission checks
   - **Out of scope**: explicitly list what is NOT included in this feature
   - **Open questions**: anything that requires a decision before implementation can begin
4. Structure the output as a concept document with exactly those six sections.

### Self-Critique

After writing the concept, review it in one short paragraph:
- Is the problem statement precise enough that a developer could start TDD without further clarification?
- Are German domain terms used consistently — no silent switches to English equivalents mid-document?
- Are any acceptance criteria vague or untestable ("system should be fast")?
- Is anything listed as "Out of Scope" that is actually a dependency — and therefore should be flagged as a blocker?

## Output Format

A markdown concept document with these sections:

```
## Hintergrund (Background)
## Domänenmodell (Domain Model)
## Akzeptanzkriterien (Acceptance Criteria)
## Randfälle (Edge Cases)
## Außerhalb des Umfangs (Out of Scope)
## Offene Fragen (Open Questions)
```

Do not write production code or tests.

