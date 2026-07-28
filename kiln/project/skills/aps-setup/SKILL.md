---
name: aps-setup
description: Install and build the Acceptance Pipeline Specification tools.
---

# APS Setup Skill

You are responsible for ensuring the Acceptance Pipeline Specification tools are ready before tests run.

## Purpose

Kiln uses the Acceptance Pipeline Specification (`gherkin-parser` and `gherkin-mutator`) to parse Gherkin feature files and mutate their parameters for acceptance test quality verification. This skill ensures both tools are installed and the project-specific acceptance pipeline components are in place.

## References

- **Repository**: `github.com/unclebob/Acceptance-Pipeline-Specification`
- **Tools**:
  - `gherkin-parser` — parses `.feature` files into an intermediate format
  - `gherkin-mutator` — mutates Gherkin parameter values to verify test quality
- **Language**: Go — build both tools from source regardless of the target project's language

## Startup Checklist

On your role's startup, verify or build the following:

1. **Install Go** (if not already installed)
   - Verify with: `go version`
   - Install from: `https://golang.org/dl/`

2. **Build `gherkin-parser`**
   ```bash
   git clone https://github.com/unclebob/Acceptance-Pipeline-Specification /tmp/aps-build
   cd /tmp/aps-build
   go build -o gherkin-parser ./cmd/gherkin-parser
   # Copy to PATH or use absolute path in project
   ```

3. **Build `gherkin-mutator`**
   ```bash
   cd /tmp/aps-build
   go build -o gherkin-mutator ./cmd/gherkin-mutator
   # Copy to PATH or use absolute path in project
   ```

4. **Verify installation**
   ```bash
   gherkin-parser --help
   gherkin-mutator --help
   ```

5. **Project-specific acceptance pipeline components**
   Ensure the following exist in your project:
   - An **acceptance entrypoint generator** — converts Gherkin scenarios into runnable test cases
   - An **acceptance runtime** — executes generated tests and reports pass/fail
   - **Step handlers** — implementations of `Given`/`When`/`Then` steps for your domain
   - A **runner adapter** — glues the runtime to your language's test framework

   These are project-specific; coordinate with the architect and coder to ensure they exist before running acceptance tests.

## Progress Reporting

When running `gherkin-mutator`, ensure the tool reports progress periodically (typically every 10-20 mutations). Long-running mutation tests without visible progress may appear to hang. Check tool output frequently.

## Role Integration

This skill is referenced by:
- **`specifier.md`**: On startup, ensure APS tools are available for running feature file tests.
- **`coder.md`**: On startup, ensure APS tools are available; verify the acceptance pipeline can parse and run scenarios.
- **`architect.md`**: On startup, build the project-specific runner adapter for `gherkin-mutator`.

---

## Troubleshooting

**`gherkin-parser not found`**
- Verify the binary is in PATH or use an absolute path in your scripts.
- Check the build completed without errors: `go build -v`.

**`gherkin-mutator hangs`**
- Ensure it reports progress every few mutations. If silent for > 5 minutes, check for infinite loops in step handlers.
- Run with a timeout: `timeout 300 gherkin-mutator ... || echo "Mutation run exceeded 5 minutes"`

**Acceptance tests fail but feature files parse correctly**
- Verify project-specific components (entrypoint generator, step handlers, runner adapter) are implemented.
- Test a single scenario in isolation: `gherkin-parser <file>.feature | entrypoint-generator | runner-adapter`.

