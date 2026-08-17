<!-- Copied into <project>/kiln/project/constitution/engineering.md during project init (kiln.ps1 -Init / kiln.sh init). Customize per project — language, build tools, test frameworks, coding practices. -->

# Engineering Rules

- On startup, acquire the github tools for the project language and get them ready to run.
- Language tool table:
  - Python: install with `pip` / `uv`; mutation `mutmut` (`pip install mutmut`), CRAP/complexity `radon` (`pip install radon`), linting `ruff` (`pip install ruff`), formatting `black` (`pip install black`), type checking `mypy` (`pip install mypy`).
- Work in small, reviewable increments.
- Prefer the simplest design that supports the current behavior and leaves clear options for the next step.
- Keep tests close to the behavior being changed.
- Separate testable modules from environmentally unsuitable modules that open GUIs, depend on external devices, throw environment errors, emit system errors, or hang under automated tests. Maximize testable code and minimize the unsuitable boundary.
- Only testable modules should participate in tools that run tests, including unit tests, acceptance tests, coverage, mutation testing, CRAP analysis, DRY analysis that invokes tests, and property tests.
- Keep property tests separate from normal verification. Do not include property-test tags in normal unit coverage, language mutation tools, CRAP, or coverage commands unless the role owns property-test verification or the user explicitly asks for property tests.
- Before running language, build, or test commands, prefer project-local cache/configuration paths inside the assigned worktree. Avoid default cache locations that write outside the project and may trigger sandbox or permission restrictions.
- Run the relevant local verification command before handoff whenever the project has one.
- Do not commit unrelated local changes or generated artifacts unless required for the task.
- Before relying on an unfamiliar command, inspect local help or project documentation.
- Pick one execution environment per worktree and stay in it. Do not fall back from the native
  toolchain to a container, a VM or WSL (or the reverse) partway through a task: files created by
  one side are frequently not writable by the other, and the failure surfaces as a permission
  error deep inside a tool rather than as a configuration problem. If the native path does not
  work, say so in the handoff instead of switching.
- Delete a tool's generated output before re-running it — mutation working copies, coverage data,
  build directories. These survive a *successful* run too, so the cycle that inherits them is
  usually not the one that produced them.
- A behaviour that returns an ordered list must name its sort key in the specification prose, not
  only in example data. "In a stable order" is not a specification: two roles will read it
  differently, both defensibly, and the disagreement surfaces as a failing acceptance test that
  neither of them can resolve alone.
