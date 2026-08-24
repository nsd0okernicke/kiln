---
name: crap-run
description: Run CRAP analysis and reduce complexity in Python projects.
---

# CRAP Analysis and Reduction Skill

You are responsible for measuring code complexity and coverage, then reducing CRAP scores below acceptable thresholds.

## Purpose

CRAP (Change Risk Anti-Patterns) combines complexity and test coverage to identify high-risk code that is both complex and under-tested. This skill ensures code changed in a cycle is sufficiently simple and well-tested before handoff.

## CRAP Measurement

**CRAP Score** = Complexity² × (1 - Coverage) + Complexity / 2

For Python, use `radon` to compute cyclomatic complexity and cross-reference with coverage data from `coverage.py`.

### Step 1: Measure Complexity

Run `radon` on modified files:

```bash
radon cc -s -n C <file.py>
```

**Flags**:
- `-s`: Sort by complexity (highest first)
- `-n C`: Show only functions with complexity ≥ C (use C=3 for initial scan, C=5 for strict assessment)

**Example output**:
```
<file.py>
    M order_processor.py:process_order 6
    C order_processor.py:validate_order 4
    F order_processor.py:calculate_discount 2
```

Interpretation:
- `M` (medium): complexity 6 — moderate risk
- `C` (complex): complexity 4 — elevated risk
- `F` (fully analyzed): complexity 2 — low risk

### Step 2: Measure Test Coverage

Run coverage on the modified code:

```bash
coverage run -m pytest tests/
coverage report --skip-covered | grep <file.py>
```

**Example output**:
```
src/order_processor.py    125    10    92%
```

Interpretation: 125 lines, 10 uncovered, 92% coverage.

### Step 3: Calculate CRAP

For each function flagged by radon:

```
CRAP = Complexity² × (1 - Coverage%) + Complexity / 2
```

**Examples**:
- `process_order` (complexity 6, coverage 85%): CRAP = 36 × 0.15 + 3 = 8.4
- `validate_order` (complexity 4, coverage 100%): CRAP = 16 × 0 + 2 = 2.0
- `calculate_discount` (complexity 2, coverage 90%): CRAP = 4 × 0.1 + 1 = 1.4

**Acceptable threshold**: CRAP ≤ 6 per function (role-defined; see `refactorer.md`).

---

## Reduction Workflow

For each function with CRAP > 6:

### Option A: Reduce Complexity

Extract logic into helper functions to lower complexity.

**Before**:
```python
def process_order(order):
    if order.status == "pending":
        if order.total > 100:
            apply_discount(order, 0.1)
        else:
            apply_discount(order, 0.05)
        if order.items:
            if all(item.in_stock for item in order.items):
                confirm_order(order)
            else:
                reject_order(order, "Item out of stock")
        else:
            reject_order(order, "No items")
    return order
```

Complexity: 6 (5 branches).

**After**:
```python
def process_order(order):
    if order.status != "pending":
        return order

    apply_discount_by_total(order)

    if not order.items:
        reject_order(order, "No items")
    elif all(item.in_stock for item in order.items):
        confirm_order(order)
    else:
        reject_order(order, "Item out of stock")

    return order


def apply_discount_by_total(order):
    rate = 0.1 if order.total > 100 else 0.05
    apply_discount(order, rate)
```

New complexity: `process_order` = 4, `apply_discount_by_total` = 2. Both ≤ 6.

### Option B: Increase Test Coverage

If complexity is unavoidable (e.g., business logic with many conditions), increase coverage to lower CRAP.

**Example**: `process_order` with complexity 6, coverage 60% → CRAP = 36 × 0.4 + 3 = 17.4 (unacceptable).

Add tests for missing branches:
```python
def test_process_order_high_value_with_discount():
    order = Order(status="pending", total=150, items=[...])
    process_order(order)
    assert order.discount_rate == 0.1


def test_process_order_low_value_with_discount():
    order = Order(status="pending", total=50, items=[...])
    process_order(order)
    assert order.discount_rate == 0.05


def test_process_order_missing_items():
    order = Order(status="pending", items=[])
    process_order(order)
    assert order.status == "rejected"
```

Coverage increases to 95% → CRAP = 36 × 0.05 + 3 = 4.8 (acceptable).

### Option C: Combination

Often the best approach: slightly reduce complexity **and** ensure all branches are tested.

---

## Refactorer Responsibility

As the `refactorer` role:

1. **Run radon and coverage on all changed files** (committed in the coder's cycle)
2. **Identify functions with CRAP > 6**
3. **For each high-CRAP function**:
   - Attempt reduction via extraction (prefer this for testability)
   - If extraction is infeasible, add tests to raise coverage
   - Verify CRAP drops to ≤ 6
4. **Re-run acceptance and unit tests** to ensure changes are behavior-preserving
5. **Commit refactoring changes** with a message linking the CRAP reduction to the function name

**Example commit**:
```
Refactor: reduce CRAP in order_processor.process_order

Extract discount logic into apply_discount_by_total() to lower
complexity from 6 to 4. Ensures all edge cases (high/low value,
out of stock, missing items) are testable and maintainable.

Before: CRAP = 8.4 (complexity 6, coverage 85%)
After:  CRAP = 2.0 for process_order
        CRAP = 1.8 for apply_discount_by_total (new helper)
```

---

## Tool Integration

### Python (radon + coverage)

```bash
# Step 1: Measure complexity
radon cc -s -n C src/

# Step 2: Run tests and measure coverage
coverage run -m pytest tests/ -v
coverage report

# Step 3: Report CRAP (manual calculation or tool)
radon mi -s src/  # Maintainability index (includes complexity)
```

### Acceptable Thresholds

- **Cyclomatic complexity**: ≤ 5 per function (preferred), ≤ 6 per function (acceptable)
- **CRAP score**: ≤ 6 per function
- **Overall coverage**: ≥ 90% on changed files

---

## Troubleshooting

**"Radon reports high complexity but the code is simple"**
- Radon counts all branches (if, for, while, and, or, except). Some "complex" code is just handling many cases.
- Judgment call: if branches are independent, extraction helps. If branches are in a single conditional logic, tests may be enough.

**"Coverage is 100% but CRAP is still high"**
- Confirm coverage is actually 100% on that function: `coverage report --skip-covered` should omit the function.
- Recount: 100% coverage makes CRAP = Complexity / 2. E.g., complexity 10, CRAP = 5.0 (acceptable).
- If CRAP > 6 with 100% coverage, complexity is the issue — extract.

**"Can't extract because the function is a template or config reader"**
- Configuration parsing and template logic legitimately need many branches.
- Acceptable to keep CRAP ≤ 6 via high coverage (95-100%) rather than extraction.
- Mark as "architectural exception" in commit message if needed.

