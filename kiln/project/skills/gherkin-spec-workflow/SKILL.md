---
name: gherkin-spec-workflow
description: Four-phase process for writing and refining Gherkin feature files.
---

# Gherkin Specification Workflow Skill

You are responsible for writing and refining Gherkin feature files aligned to the Acceptance Pipeline Specification.

## Purpose

This skill orchestrates the four-phase process for creating acceptance tests: write the scenario, prune parameters, consolidate setup, and prepare for mutation testing. The result is concise, mutation-aware Gherkin that serves as both a specification and a quality gate.

## Format and Repository

**Repository**: `github.com/unclebob/Acceptance-Pipeline-Specification`

Follow the Gherkin format defined by the APS, which standardizes:
- Scenario structure: `Given` (setup), `When` (action), `Then` (assertion)
- Parameter syntax: `<parameter-name>` for values that vary across scenarios
- Feature file organization: one behavior per feature file, grouped by technology or domain

## Four-Phase Workflow

Run each phase in order. Each phase builds on the previous one.

---

### Phase 1: Write the Gherkin

**Goal**: Capture the complete behavior in Gherkin, including all relevant variations.

**Steps**:
1. Read the user's requirement or issue description
2. Identify the happy path scenario and 2-3 edge case scenarios
3. Write each scenario in full: `Feature` header, `Scenario`, `Given`/`When`/`Then` steps
4. Include all relevant values inline (e.g., `5`, `"John"`, `true`)

**Example** (before optimization):

```gherkin
Feature: Order processing

  Scenario: Order with sufficient inventory
    Given inventory has 10 widgets
    When customer orders 5 widgets
    Then order is confirmed
    And customer is charged $50

  Scenario: Order with no inventory
    Given inventory has 0 widgets
    When customer orders 1 widget
    Then order is rejected
    And error message is "Out of stock"
```

**Checklist**:
- [ ] Each scenario tests one behavior
- [ ] Scenarios include boundary cases (zero, one, max, etc.)
- [ ] No implementation details leak into steps (e.g., "call `order_service.submit()`" is wrong; "customer orders" is right)
- [ ] German domain language used for business concepts; English for technical step verbs (`Given`, `When`, `Then`)

---

### Phase 2: Prune Parameters

**Goal**: Extract values that vary across scenarios into parameters; keep values that are invariant or test-specific inline.

**Guideline**: A parameter belongs in a `<scenario-parameter>` placeholder only if:
- It varies **across scenarios** in this feature (different test cases use different values)
- It is **relevant to Gherkin mutation testing** (mutation will vary it to check test quality)

Values that should **stay inline**:
- Dates, times, or context that is the same across all scenarios in the feature
- Implementation details (table names, API endpoints, database IDs)
- Values where variation is tested elsewhere

**Example** (after pruning):

```gherkin
Feature: Order processing

  Scenario: Order with sufficient inventory
    Given inventory has <inventory> widgets
    When customer orders <quantity> widgets
    Then order is <status>
    And customer is charged <price>

  Scenarios:
    | inventory | quantity | status     | price |
    | 10        | 5        | confirmed  | $50   |
    | 10        | 1        | confirmed  | $10   |
    | 0         | 1        | rejected   | $0    |
```

**Mutation-aware pruning**: Each parameter in a Gherkin table will be mutated independently. Gherkin-mutator will create scenarios with `inventory=11`, `quantity=6`, `status=pending`, etc., and verify that your acceptance tests catch the differences.

**Checklist**:
- [ ] Each parameter has 2-4 distinct values in the table (mutation needs variety to be effective)
- [ ] No redundant parameters (e.g., don't parameterize both `price` and `quantity` if one is always derived from the other)
- [ ] Inline values are stable across all scenarios (dates, endpoints, etc.)

---

### Phase 3: Extract Background

**Goal**: Move repeated `Given` steps into a `Background` section if they preserve scenario meaning.

**Guideline**: A `Given` step belongs in `Background` only if:
- It appears in **most or all scenarios** in the feature
- Removing it from individual scenarios **does not make the test harder to understand**

Do **not** use `Background` for:
- Conditional setups (only needed in some scenarios)
- Critical context that helps readers understand each scenario (leave it inline for clarity)

**Example** (after extracting Background):

```gherkin
Feature: Order processing

  Background:
    Given the order system is operational
    And customer authentication is enabled

  Scenario: Order with sufficient inventory
    Given inventory has <inventory> widgets
    When customer orders <quantity> widgets
    Then order is <status>
    And customer is charged <price>

  Scenarios:
    | inventory | quantity | status     | price |
    | 10        | 5        | confirmed  | $50   |
    | 0         | 1        | rejected   | $0    |
```

**Checklist**:
- [ ] Background steps are factual, not opinionated (setup facts, not value judgments)
- [ ] Each scenario still makes sense when read alone (mentally re-add the Background)
- [ ] No more than 3-5 steps in Background (keep it focused)

---

### Phase 4: Approval Gate

**Goal**: Present the refined feature file to the user for approval before handing off to the coder.

**Steps**:
1. Render the complete feature file (Gherkin with Background and Scenarios)
2. Show the mutation-aware parameter table
3. Ask the user: "Does this specification match the intended behavior?"

**User feedback options**:
- ✅ **Approved**: Proceed to commit and handoff
- 🔄 **Adjust parameters**: User wants different test values or fewer parameterizations
- 📝 **Add scenarios**: User wants additional edge cases (loop back to Phase 1)
- ❌ **Clarify wording**: User wants step verbiage changed (loop back to Phase 1)

**Commit message** (on approval):
```
Spec: <feature-name> — Phase 1-4 workflow complete

Describes: <one-sentence behavior>

Scenarios:
- <happy path>
- <edge case 1>
- <edge case 2>

Parameters: <list parameters and why they vary>
Mutation-ready: <estimated mutation sites = scenario count × parameter count>
```

**Handoff to coder**:
- Invent a short stable handoff name (e.g., `order-processing-v1`)
- File name: `features/<feature-name>.feature` (at project root, not in kiln/)
- Send handoff with: feature file path, parameter table, and approval timestamp

---

## Best Practices

### German + English Language Mix

- **German**: Business domain terms and concepts (e.g., `Bestellung`, `Lagerbestand`, `Rechnung`)
- **English**: Technical step verbs (`Given`, `When`, `Then`), parameter placeholders, assertion operators

Example:
```gherkin
Scenario: Bestellung wird abgelehnt bei leerem Lagerbestand
  Given Lagerbestand für Artikel "Widget" ist <bestand>
  When Kunde versucht Bestellung aufzugeben für <menge> Stück
  Then Bestellung ist <status>
  And Fehler "Lagerbestand unzureichend" wird angezeigt
```

### Avoid Implementation Leakage

❌ **Wrong** (implementation detail):
```gherkin
Given the OrderService instance is created with config `{"max_items": 100}`
When OrderService.submit({"id": 123, "qty": 5}) is called
Then the response JSON contains "order_id"
```

✅ **Right** (behavior-focused):
```gherkin
Given maximum order size is 100 items
When customer submits order for 5 items
Then order is created with a unique ID
```

### Mutation Readiness

Gherkin parameters will be mutated by gherkin-mutator to check test quality:

```gherkin
Given inventory is <inventory> units  ← mutator will try 11, 9, 100, etc.
When customer orders <quantity>       ← mutator will try quantity+1, quantity-1, etc.
Then total cost is <price>            ← mutator will try price+0.01, price*2, etc.
```

Your acceptance tests must **detect** when a parameter changes (i.e., test must fail when mutated value replaces original). This ensures the test is actually exercising the code path that depends on that parameter.

---

## Troubleshooting

**"Parameters are too similar across scenarios"**
- If `inventory: [10, 11, 12]`, mutation barely moves the needle. Use more contrasting values: `[0, 5, 10, 100]`
- Ensure parameter values represent distinct test cases (boundary, happy path, error case)

**"User says 'too many scenarios'"**
- Consolidate similar edge cases into one parameterized scenario
- Keep only behaviors that are distinctly different (not just different parameter values of the same logic)

**"Feature file is > 50 lines"**
- Split into multiple feature files by technology or domain boundary
- Each file should focus on one behavior family

**"Mutation tool reports 0 survivors but we know tests are weak"**
- Verify scenarios actually run with the acceptance entrypoint generator
- Check that `Then` steps make assertions on the mutated parameters (not just side effects)
- Ensure test runner reports failure when assertions fail

