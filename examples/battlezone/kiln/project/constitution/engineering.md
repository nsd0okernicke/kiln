<!-- Copied into <project>/kiln/project/constitution/engineering.md during project init (kiln.ps1 -Init / kiln.sh init). Example-specific override for battlezone — adds the pygame/headless-testing notes the framework's generic Python default doesn't know about. -->

# Engineering Rules

- On startup, acquire the tools for the project language and get them ready to run.
- Language tool table:
  - Python: install with `pip` / `uv`; mutation `mutmut` (`pip install mutmut`), CRAP/complexity `radon` (`pip install radon`), linting and formatting `ruff` (`pip install ruff` — covers both `ruff check` and `ruff format`, no separate `black` dependency), type checking `mypy` (`pip install mypy`), rendering/input `pygame` (`pip install pygame`).
- Work in small, reviewable increments.
- Prefer the simplest design that supports the current behavior and leaves clear options for the next step.
- Keep tests close to the behavior being changed.
- **This project's environmentally-unsuitable boundary is `battlezone/infrastructure/`** — it opens a real pygame window and polls real keyboard input, so it hangs or errors under automated test tools. `battlezone/domain/` and `battlezone/application/` must never import `pygame`; if a change seems to require that, the design is wrong — introduce a new pure boundary type (like `FrameState` or an input-intent enum) instead of reaching for a `pygame` type inside the testable layers.
- Only testable modules (`domain/`, `application/`) should participate in tools that run tests, including unit tests, acceptance tests, coverage, mutation testing, CRAP analysis, and property tests. `infrastructure/` is verified by manual playtest only (see `constitution/project.md` → "Local Run").
- Keep property tests separate from normal verification. Do not include property-test tags in normal unit coverage, language mutation tools, CRAP, or coverage commands unless the role owns property-test verification or the user explicitly asks for property tests.
- If a test ever does need to import `pygame` (should be rare — acceptance tests drive `GameSession` directly and never touch it), set `SDL_VIDEODRIVER=dummy` in the environment first so pygame doesn't attempt to open a real window in a sandboxed/headless agent environment. Treat needing this as a signal to double-check the domain/application boundary wasn't accidentally crossed.
- Before running language, build, or test commands, prefer project-local cache/configuration paths inside the assigned worktree. Avoid default cache locations that write outside the project and may trigger sandbox or permission restrictions.
- Run the relevant local verification command before handoff whenever the project has one.
- Do not commit unrelated local changes or generated artifacts unless required for the task.
- Before relying on an unfamiliar command, inspect local help or project documentation.
