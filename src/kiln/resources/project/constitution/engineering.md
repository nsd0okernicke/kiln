<!-- Copied into <project>/kiln/project/constitution/engineering.md during project init (kiln.ps1 -Init / kiln.sh init). Customize per project — language, build tools, test frameworks, coding practices. -->

# Engineering Rules

- On startup, acquire the github tools for the project language and get them ready to run.
- Language tool table:
  - Python: install with `pip` / `uv`; mutation `cosmic-ray` (`pip install cosmic-ray`), CRAP/complexity `radon` (`pip install radon`), linting `ruff` (`pip install ruff`), formatting `black` (`pip install black`), type checking `mypy` (`pip install mypy`).
    - Deliberately not `mutmut`: it refuses to start on native Windows and prints "please use
      the WSL" (boxed/mutmut#397). The "one execution environment per worktree" rule below
      forbids the only workaround it offers, so on Windows the two rules together make the
      mutation gate unsatisfiable. `cosmic-ray` runs natively everywhere Kiln does.
  - Quality gates (Python):
    - **Dependency scanning** — `pip-audit` (`pip install pip-audit`) or `safety` for known-vulnerability scans.
    - **Documentation coverage** — `interrogate` (`pip install interrogate`) to check public-interface docstrings.
    - **Import enforcement** — `import-linter` (`pip install import-linter`) to enforce dependency direction rules (domain must not import infrastructure).
    - **SAST / security scanning** — `bandit[sarif]` (`pip install bandit[sarif]`) for static analysis of changed files with SARIF output.
    - **Complexity analysis** — `radon` (`pip install radon`) for cyclomatic complexity and maintainability index.
  - Declare every quality-gate tool in `[project.optional-dependencies] dev` in `pyproject.toml` so a fresh install (`pip install -e ".[dev]"`) brings in the full gate toolchain. Tools left out of the declared dependencies vanish silently on environment rebuild.
- **A gate measures the code. When they disagree, the code is what changes.** Never edit shipped
  code to move a metric — that inverts what the gate is for, and it ships a change no
  code-quality reason justifies. The distinction is the *purpose* of the edit, not its size:
  rewriting an annotation, renaming a symbol or restructuring a branch is fine when it improves
  the code, and not fine when the reason in the commit message is "so the tool stops counting
  it".
  - Every gate tool has a supported way to exclude what it should not measure — an operator
    filter, an ignore list, an exclusion path. Use it, and write down *why* next to it.
  - **Equivalent mutants** are the common case. A mutant no test can kill because the mutated
    expression is never evaluated (a type annotation under `from __future__ import annotations`)
    or because both forms behave identically (`is` versus `==` on enum members, `> N` versus
    `>= N` on a clamp that is a no-op at `N`) is excluded at the mutation tool, by operator or
    by module, never by rewriting the production expression it lands on.
  - If a gate cannot be satisfied without changing shipped code for the tool's benefit, that is
    a finding for the handoff, not a task for the editor.
- **Narrowing a gate is a change to the gate.** Reducing a lint rule set, adding an entry to a
  coverage `omit` list, excluding a module from mutation, or lowering a threshold all make the
  gate report green about less than it did. Any of them may be right — but state it in the
  handoff with the reason, because none of them is visible in a passing result. Never widen an
  exclusion to make a failing gate pass.
- Work in small, reviewable increments.
- Prefer the simplest design that supports the current behavior and leaves clear options for the next step.
- Keep tests close to the behavior being changed.
- Separate testable modules from environmentally unsuitable modules that open GUIs, depend on external devices, throw environment errors, emit system errors, or hang under automated tests. Maximize testable code and minimize the unsuitable boundary.
- Only testable modules should participate in tools that run tests, including unit tests, acceptance tests, coverage, mutation testing, CRAP analysis, DRY analysis that invokes tests, and property tests.
- Keep property tests separate from normal verification. Do not include property-test tags in normal unit coverage, language mutation tools, CRAP, or coverage commands unless the role owns property-test verification or the user explicitly asks for property tests.
- **Never skip inside a property test to discard an unwanted example — filter instead.** In
  Hypothesis, `pytest.skip()` inside an `@given` function aborts the *entire property* the first
  time it is reached, so a test meant to check 200 examples silently checks the handful drawn
  before the first skip. Use `hypothesis.assume(...)`, which rejects that one example and keeps
  the property running. The same holds for any generative framework: reject the example, do not
  end the test. A property that reports as skipped has not been weakened, it has stopped.
- Before running language, build, or test commands, prefer project-local cache/configuration paths inside the assigned worktree. Avoid default cache locations that write outside the project and may trigger sandbox or permission restrictions.
- Run the relevant local verification command before handoff whenever the project has one.
- Do not commit unrelated local changes or generated artifacts unless required for the task.
- Before relying on an unfamiliar command, inspect local help or project documentation.
- Pick one execution environment per worktree and stay in it. Do not fall back from the native
  toolchain to a container, a VM or WSL (or the reverse) partway through a task: files created by
  one side are frequently not writable by the other, and the failure surfaces as a permission
  error deep inside a tool rather than as a configuration problem. If the native path does not
  work, say so in the handoff instead of switching.
- Prefer an invocation that needs no shell state. Do not rely on a prior command having changed
  the environment: `activate` followed by `pytest` is one more thing that can hang or silently
  not apply, and when it does the failure appears in an unrelated command much later.
  - **A command you type yourself** may name the interpreter by path:
    `.venv\Scripts\python.exe -m pytest`.
  - **A command you write into a committed file** — a mutation `test-command`, a CI step, a
    script — must use the environment-resolving form instead: `uv run python -m pytest`. It
    needs no shell state *and* it still resolves correctly from another worktree, another
    checkout, or a CI runner.
  - A bare `python` is not a substitute for either. It resolves to whatever the host's PATH
    prefers, which is routinely a system interpreter without the project's dependencies. Under
    mutation testing that failure is silent in the worst possible way: every mutant "dies" of
    the same collection error and the tool reports a perfect score.
  - Never commit an absolute path to an interpreter. It encodes one machine into a file every
    other machine has to run, and it makes the result unreproducible for the next reader. If a
    committed command needs a specific interpreter, it needs `uv run`.
- A command that has not finished is not a command that needs more waiting. If you are polling
  something you started and it has not progressed after a few checks, stop, kill it, and report
  what it was — do not keep polling. Observed live: a worker recognised its own hung step
  ("a shell activation issue, not a test failure") and then polled it another 30 times until
  the cycle died with nothing handed off.
- A test suite that depends on an external runtime — a container engine, a database server, a
  message broker — must probe for it before running, and if it is absent, skip that suite and
  state the gap in the handoff. Do not let the suite discover this for itself: a container
  fixture waits on the daemon indefinitely rather than failing, so the run does not error, it
  simply stops, and the worker is killed by its timeout with nothing to show. `docker info`
  is the probe for a container engine.
- Delete a tool's generated output before re-running it — mutation working copies, coverage data,
  build directories. These survive a *successful* run too, so the cycle that inherits them is
  usually not the one that produced them.
- A behaviour that returns an ordered list must name its sort key in the specification prose, not
  only in example data. "In a stable order" is not a specification: two roles will read it
  differently, both defensibly, and the disagreement surfaces as a failing acceptance test that
  neither of them can resolve alone.
  - **Name the direction, and name a tie-break.** "Sorted by `due_date`" is half a
    specification — ascending and descending are both consistent with it. And a single key is
    only a *partial* order: records created in one test transaction routinely share a
    timestamp, so with no second key the expected order is decided by insertion order, which
    nobody specified and no reviewer can check. Every ordered result needs
    `<key> <direction>, tie-broken by <unique key> <direction>`.
  - **The prose is normative; the example table must agree with it.** Where they conflict, the
    table is wrong — but do not rely on that rule to rescue you, because the coder cannot edit
    a specifier-owned feature file and the cycle stalls. Check each row against the prose
    before handing off.
