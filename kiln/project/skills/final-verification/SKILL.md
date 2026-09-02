---
name: final-verification
description: Run the three-step final verification sequence before handoff.
---

# Final Verification Skill

You are responsible for the comprehensive pre-handoff quality verification sequence.

## Purpose

Before handing off completed work to the next role, the architect runs a strict three-step verification sequence to ensure all quality gates pass. This skill orchestrates those steps in the correct order and ensures issues are fixed before advancing.

## Three-Step Sequence

Run each step in order. **Do not proceed to the next step until the current one passes.**

### Step 1: Mutation Testing

Run the mutation test suite on all changes (see `run-mutation` skill for detailed protocol):

```bash
# For each modified file:
<mutation-tool> --mutate <file> --manifest mutations.xml --max-workers 8 --verbose --progress
```

**Pass criteria**: All survivors are killed OR categorized with targeted tests written to address them.

**Fail action**: 
- If survivor count is high, consult the `run-mutation` skill's survivor analysis
- Write or strengthen tests for each survivor category
- Re-run mutation tests until pass criteria met

---

### Step 2: DRY (Don't Repeat Yourself) Analysis

Run the DRY analysis tool on all changes:

```bash
# Python example (see constitution/engineering.md for your language)
radon mi -n C src/
```

**Pass criteria**: All duplication is above the threshold (typically 3-5 occurrences required before refactoring).

**Fail action**:
- Refactor duplicated code into shared helpers or utility functions
- Ensure refactored code is behavior-preserving (acceptance and unit tests must still pass)
- Re-run DRY analysis until pass criteria met

---

### Step 3: Soft Gherkin Acceptance Mutation

Run a **soft** (parameter-only) mutation on the Gherkin acceptance tests to verify they catch parameter changes:

```bash
gherkin-mutator --level soft --mutate <feature-files> --manifest gherkin-mutations.xml --verbose
```

**Pass criteria**: Soft mutation detects at least 80% of parameter changes (i.e., < 20% survivors).

**Fail action**:
- Review Gherkin scenarios with high survivor rates
- Add or clarify `When` steps to ensure parameters are actually used in assertions
- Adjust `Then` steps to be more specific (e.g., "the price is exactly 19.95" vs "the price is calculated")
- Re-run soft Gherkin mutation until pass criteria met

---

## Handoff Readiness Checklist

After all three steps pass, verify:

- [ ] Mutation testing: all survivors killed or analyzed with tests added
- [ ] DRY analysis: duplication refactored or justified
- [ ] Soft Gherkin mutation: acceptance tests catch parameter changes
- [ ] All acceptance tests pass: `gherkin-parser <features> | runner --all`
- [ ] All unit tests pass: `<test-runner> --coverage` 
- [ ] Code review: module structure, testability, boundaries all sound
- [ ] Changelog or commit messages document the changes

---

## What Happens If a Step Fails

**Do not skip to the handoff.** Instead:

1. Identify which step failed
2. Fix the underlying issue (add tests, refactor duplication, clarify Gherkin scenarios)
3. Re-run **only that step** (not all three from the start)
4. Verify the fix did not break earlier steps (spot-check mutation or DRY if uncertain)
5. Resume from the failed step and complete the remaining steps

This prevents cascading issues and ensures each quality gate is independently satisfied.

---

## Performance Expectations

| Step | Typical Duration |
|---|---|
| Mutation testing | 15-45 min (depends on test suite and module size) |
| DRY analysis | 1-5 min |
| Soft Gherkin mutation | 5-15 min |

If any step takes significantly longer:
- Mutation: check if test suite is slow or module is too large (> 100 sites) — split the module
- DRY: likely okay; duplication detection is I/O limited, not compute
- Gherkin: check feature file count and scenario complexity

---

## Integration with Architect Role

This skill is invoked by `architect.md` immediately before notifying the next role. The architect:
1. Merges completed coder/reviewer handoffs
2. Applies architectural review
3. Runs this final verification sequence
4. Only on pass, commits and notifies downstream roles

---

## Troubleshooting

**"Soft Gherkin mutation has too many survivors"**
- Review high-survivor scenarios: do they have enough assertions?
- Example: `Then the order is confirmed` (too vague) vs `Then the order total is $X and status is "confirmed"` (specific)
- Gherkin parameters (e.g., `<price>`) must be explicitly checked in `Then` steps

**"Mutation run timed out"**
- Check test suite performance: `time <test-runner> <test-file>` should complete in < 2min
- If tests are slow, mutation will be slow. Consider test optimization as a separate refactoring task

**"DRY tool reports false positives"**
- Some repeated patterns are okay (e.g., boilerplate, configuration). Use tool's exclusion/configuration options
- Judgment call: refactor if duplication makes future changes risky, leave as-is if isolated and stable

