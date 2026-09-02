---
name: property-test-generator
description: Generate property-based tests for key invariants and edge cases.
---

# Property-Based Test Generator Skill

You are an expert at generating strong, randomised tests for domain invariants.

## Purpose
- Identify domain objects and functions that benefit from property-based testing.
- Create tests that cover a broad space of valid and invalid inputs.

## Conventions
- Use the selected config's preferred property-testing library.
- Focus on invariants, validation rules, and transformation behavior.
- Keep tests readable and maintainable.

## Instructions

### Phase 1: Domain Type Discovery

1. Read the active config's `MEMORY.md` for the property-testing library (`kotest Arb` for Kotlin, `hypothesis st` for Python, `fast-check fc` for React/TypeScript) and the domain package path.
2. Scan the `domain/` package (or `src/` for React) for domain types:
   - Kotlin: `data class`, `value class`, classes with `init { require(...) }` guards
   - Python: `@dataclass`, `NamedTuple`, classes with `__post_init__` validation
   - React/TypeScript: `interface`, `type`, classes or factory functions with validation logic
3. For each type, extract fields, field types, and any visible constraints (range guards, regex, non-null, non-empty).

### Phase 2: Arb / Strategy Mapping

4. Map each field to a generator based on its type and constraints:

   **Kotlin → Kotest Arb**

   | Field type | Generator |
   |---|---|
   | `String` (unconstrained) | `Arb.string(minSize = 1)` |
   | `String` (email / name) | `Arb.email()`, `Arb.name()` |
   | `Int` / `Long` (positive) | `Arb.positiveInt()`, `Arb.positiveLong()` |
   | `Int` in range `a..b` | `Arb.int(a..b)` |
   | `BigDecimal` (monetary) | `Arb.bigDecimal(min..max)` |
   | `LocalDate` | `Arb.localDate()` |
   | `Enum` | `Arb.enum<MyEnum>()` |
   | Custom domain type | `Arb.bind(arbField1, arbField2) { a, b -> MyType(a, b) }` |
   | Constrained field | wrap with `.filter { constraint }` |

   **Python → Hypothesis strategies**

   | Field type | Strategy |
   |---|---|
   | `str` (unconstrained) | `st.text(min_size=1)` |
   | `str` (email) | `st.emails()` |
   | `int` (positive) | `st.integers(min_value=1)` |
   | `int` in range | `st.integers(min_value=a, max_value=b)` |
   | `Decimal` (monetary) | `st.decimals(min_value=0, places=2)` |
   | `date` | `st.dates()` |
   | `Enum` | `st.sampled_from(MyEnum)` |
   | Custom domain type | `st.builds(MyType, field=strategy, ...)` |
   | Constrained field | wrap with `st.assume(constraint)` or `.filter(constraint)` |

   **React/TypeScript → fast-check arbitraries**

   | Field type | Arbitrary |
   |---|---|
   | `string` (unconstrained) | `fc.string({ minLength: 1 })` |
   | `string` (email) | `fc.emailAddress()` |
   | `number` (positive) | `fc.integer({ min: 1 })` |
   | `number` in range | `fc.integer({ min: a, max: b })` |
   | `boolean` | `fc.boolean()` |
   | `Date` | `fc.date()` |
   | Union/enum | `fc.constantFrom(...values)` |
   | Object/record | `fc.record({ field: arb, ... })` |
   | Array | `fc.array(itemArb, { minLength: 1 })` |
   | Constrained field | wrap with `.filter(constraint)` |

### Phase 3: Invariant Identification

5. For each domain type, derive testable invariants from the constraints you found:
   - **Constructor guard**: valid inputs always construct; guard-violating inputs always raise
   - **Roundtrip**: `parse(serialize(x)) == x`
   - **Transformation**: e.g. `Bestellung.gesamtpreis() == items.sumOf { it.preis }`
   - **Monotonicity**: if `a > b` then `f(a) > f(b)` (where applicable)
   - **Boundary**: the exact boundary value that separates valid from invalid

### Phase 4: Test Generation

6. Write one `forAll` / `@given` property test per invariant. Each test must:
   - Use the Arb/strategy discovered in Phase 2
   - Assert the invariant holds for all generated values
   - Include a `@DisplayName` (Kotlin), docstring (Python), or `test('...')` description (React/TypeScript) in German naming the invariant

### Self-Critique

After writing the tests, review your output and write one short paragraph:
- Did you cover all relevant domain types, or skip any that looked "simple"?
- Are the invariants domain-meaningful, not just "object can be constructed"?
- Are generators constrained enough to produce interesting near-boundary values?
- Is there at least one test that verifies an invalid input is rejected?

## Output Format

For each domain type: the Arb/strategy factory function, then the property tests.

### JSON Status Block

```json
{
  "skill": "property-test-generator",
  "domain_types_discovered": 0,
  "invariants_tested": 0,
  "arbs_generated": 0,
  "critique": "<one-line self-critique>",
  "status": "complete"
}
```

## If Tools Are Unavailable

- If the domain package cannot be located, ask for the path or check `MEMORY.md` for the project structure.
- If no domain types with constraints are found, state this explicitly — do not generate generic no-op tests.
- If the property-testing library is not in the build file, recommend adding it and halt.

