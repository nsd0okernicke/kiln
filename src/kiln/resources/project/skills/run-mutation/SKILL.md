---
name: run-mutation
description: Execute mutation testing with progress tracking and survivor analysis.
---

# Mutation Testing Run Skill

You are responsible for running mutation tests on code changes and analyzing surviving mutants.

## Purpose

Mutation testing validates that your test suite can detect changes (mutations) to production code. This skill ensures mutation runs are efficient, observable, and produce actionable survivor reports for targeted test improvements.

## Protocol

### 1. Scope to Changed Files First

Before running any mutation, determine which files actually changed:

```bash
git diff --name-only main...HEAD -- catalog/domain/ catalog/application/
```

- **Only domain/application files changed** → mutate those files only
- **Only infrastructure, tests, or feature files changed** → **skip mutation entirely** (infra is excluded from mutation by config; unit tests mock ports and acceptance tests are too slow)
- **No relevant files changed at all** → skip mutation, report "no domain/application changes to mutate"

### 2. Create a Scoped Config

Create a mutation config scoped to the changed files rather than the whole module:

```toml
[cosmic-ray]
module-path = "changed/file.py"
timeout = 60.0
excluded-modules = []
test-command = "pytest tests/unit -x -q"

[cosmic-ray.distributor]
name = "multiprocessing"
workers = 4
```

### 3. Run Differential (cr-filter-git)

After `cosmic-ray init`, filter the session to only sites in code this cycle touched:

```bash
cosmic-ray init mutation.toml mutation.sqlite
cr-filter-git mutation.sqlite          # Drop sites not touched by this cycle
cosmic-ray exec mutation.toml mutation.sqlite  # Resumable: re-run continues where it stopped
```

This avoids re-testing mutations in code that didn't change — the single biggest speedup.

### 4. Parallel Execution

Always use `--max-workers` or the `multiprocessing` distributor:

```toml
[cosmic-ray.distributor]
name = "multiprocessing"
workers = 4
```

On a 4-8 core machine this gives 2-4× speedup.

### 5. Verbose and Progress Output

```bash
cosmic-ray exec mutation.toml mutation.sqlite --verbose
```

### 6. Survivor Analysis

```bash
cr-rate mutation.sqlite                          # survival rate
cr-report --surviving-only mutation.sqlite       # the survivors themselves
```

**Analysis steps**:
- Group surviving mutants by class/function
- For each group, identify what test would kill the mutant (e.g., boundary value, return value check, exception type)
- Recommend which tests to add or strengthen
- Report findings in the handoff

### 6. Module Size Check

If a single file has > 100 mutation sites (reported by `--scan`):

```bash
<mutation-tool> --scan <file>  # Reports: "120 mutation sites"
```

**Action**: Coordinate with the coder or reviewer to split the module into smaller units before the next mutation run. Large modules are hard to test comprehensively and produce too many survivors to act on.

## Example Workflow

```bash
# Step 1: Describe the session. One config per package under test -- `module-path` is
# singular, so a project with several top-level packages needs several sessions.
cat > mutation.toml <<'TOML'
[cosmic-ray]
module-path = "<package>"
timeout = 60.0
excluded-modules = []
test-command = "pytest tests/unit -x -q"

[cosmic-ray.distributor]
name = "multiprocessing"
workers = 4
TOML

# Step 2: Enumerate every mutation site into a session database
cosmic-ray init mutation.toml mutation.sqlite

# Step 3: Keep the run differential -- drop sites in code this cycle did not touch
cr-filter-git mutation.sqlite

# Step 4: Execute. Resumable by design: re-running continues where it stopped rather
# than starting over, which is what makes a long run survive an interrupted shell call.
cosmic-ray exec mutation.toml mutation.sqlite

# Step 5: Summarize for handoff
cr-rate mutation.sqlite                          # survival rate
cr-report --surviving-only mutation.sqlite       # the survivors themselves
```

## Language and Tool Mapping

The actual command names depend on your project's mutation tool (see `constitution/engineering.md` for your language):

| Language | Tool | Command |
|---|---|---|
| Python | `cosmic-ray` | `cosmic-ray init <config>.toml <session>.sqlite` → `cosmic-ray exec <config>.toml <session>.sqlite` |
| Java/Kotlin | PIT (Pitest) | `./gradlew pitest -Dpitest.targetClasses=<class>` |
| Go | `stryker` or equiv | `stryker run --target <package>` |

Adapt the flags and output parsing to your tool, but follow the protocol: sequential, differential, parallelized, verbose, per-file analysis.

## Integration with Roles

This skill is referenced by:
- **`architect.md`**: Runs mutation on every completed coder/reviewer handoff; verifies all test suites catch intentional code changes.

---

## Troubleshooting

**Codex: mutation run times out around 600 seconds ("partial cache... not trustworthy")**
- This is Codex's own shell tool, not the mutation tool: each shell call defaults to `timeout_ms: 600000`
  (10 minutes) unless the command explicitly requests more. A whole-project mutation run
  easily exceeds that as a single call.
- Preferred fix: follow the **Sequential Per-File Mutation** protocol above so each individual
  run comfortably fits under the default — this also gives better progress visibility.
- If a single call genuinely needs more time anyway, set `timeout_ms` explicitly on that shell
  call (e.g. `1800000` for 30 minutes) rather than relying on the default.

**Mutation run is slow (> 1 hour per file)**
- Verify `--max-workers` is set to your core count: `nproc` (Linux/macOS) or Task Manager (Windows)
- Check if the test suite itself is slow: `time <test-runner> <test-file>` — if tests take > 1min each, mutation will be slow
- Consider splitting the module (> 100 mutation sites indicator)

**No survivors reported, but tests are weak**
- Verify all tests are actually running: check test output for assertion counts
- Confirm mutation tool is correctly parsing your code syntax (e.g., Python AST parsing for indentation-sensitive changes)
- Re-run with `--verbose` to see which mutations are generated and killed

**Survivor count remains high across cycles**
- Prioritize survivors by class (most-survived first)
- Write boundary value tests and exception handling tests — these often catch more survivors
- Check if the code is genuinely hard to test (long methods, tightly coupled modules) — may need refactoring first

