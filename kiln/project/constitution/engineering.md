<!-- Copied into <project>/kiln/project/constitution/engineering.md by kiln-init. Customize per project — language, build tools, test frameworks, coding practices. -->

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
