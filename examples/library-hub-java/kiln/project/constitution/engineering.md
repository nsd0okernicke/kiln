<!-- Copied into <project>/kiln/project/constitution/engineering.md during project init (kiln.ps1 -Init / kiln.sh init). Example-specific override for library-hub-java — Java/Spring/Maven tool table in place of the framework's Python default. -->

# Engineering Rules

- On startup, acquire the tools for the project language and get them ready to run.
- Language tool table:
  - Java: build/dependency management via the committed Maven wrapper (`./mvnw`); mutation `pitest` (plugin `org.pitest:pitest-maven`); CRAP/complexity via PIT's built-in CRAP metric (threshold 30, not radon — see the `crap-analyzer` skill's "Threshold Note"); linting/formatting `maven-checkstyle-plugin` or `spotless-maven-plugin`; coverage `jacoco-maven-plugin`; acceptance/BDD `cucumber-java` + `cucumber-junit-platform-engine`; property testing `jqwik`; layering enforcement `archunit`.
- Work in small, reviewable increments.
- Prefer the simplest design that supports the current behavior and leaves clear options for the next step.
- Keep tests close to the behavior being changed.
- Separate testable modules from environmentally unsuitable modules that open GUIs, depend on external devices, throw environment errors, emit system errors, or hang under automated tests. Maximize testable code and minimize the unsuitable boundary.
- Only testable modules should participate in tools that run tests, including unit tests, acceptance tests, coverage, mutation testing, CRAP analysis, DRY analysis that invokes tests, and property tests.
- Keep property tests separate from normal verification. Do not include property-test tags in normal unit coverage, language mutation tools, CRAP, or coverage commands unless the role owns property-test verification or the user explicitly asks for property tests.
- Before running language, build, or test commands, prefer project-local cache/configuration paths inside the assigned worktree (e.g. point `-Dmaven.repo.local` at a worktree-local directory if the shared `~/.m2/repository` causes lock contention across parallel agent worktrees). Avoid default cache locations that write outside the project and may trigger sandbox or permission restrictions.
- Run the relevant local verification command before handoff whenever the project has one.
- Do not commit unrelated local changes or generated artifacts (`target/`) unless required for the task.
- Before relying on an unfamiliar command, inspect local help (`./mvnw help:describe`) or project documentation.
