# Project Rules — LibraryHub

## Language & Tooling

- Language: Python 3.10+
- Package manager: `uv` (preferred) or `pip`
- Do not change another role's prompt or workflow ownership without explicit user direction.

## Python Virtual Environment

The project uses a shared virtual environment at `<project_root>/.venv`.

- Find project root by walking up from your worktree to the directory containing `.kiln/`.
- **On first startup**: if `.venv` does not exist at project root, create it:
  - Windows: `python -m venv <project_root>\.venv`
  - Unix: `python -m venv <project_root>/.venv`
- **Call that environment's interpreter directly. Do not activate it**:
  - Windows: `<project_root>\.venv\Scripts\python.exe -m pytest`
  - Unix: `<project_root>/.venv/bin/python -m pytest`

  Activation exists to mutate shell state, and it does so unreliably: in a non-interactive
  shell `Scripts\activate` can hang rather than return, which stalls the cycle with no error
  to report and no failing command to point at. Observed live -- a coder polled a hung
  activation 63 times across 20 minutes, correctly identified it as "a shell activation
  issue, not a test failure", and still never reached dependency installation. Naming the
  interpreter needs no shell state and cannot hang.
- Install dependencies once after creation:
  `<project_root>\.venv\Scripts\python.exe -m pip install -e ".[dev]"` (Windows) or
  `<project_root>/.venv/bin/python -m pip install -e ".[dev]"` (Unix)
- **Do NOT create a new `.venv` inside your worktree.**

Throughout the rest of this document, **`python` means that interpreter** — `python -m pytest`,
`python -m mypy`, `python -m ruff`. Tools with no module entry point (`cosmic-ray`, `cr-rate`)
are run from `<project_root>\.venv\Scripts\` on Windows, `<project_root>/.venv/bin/` on Unix.
A bare `pytest` resolves against `PATH`, which without activation is the wrong interpreter or
none at all.

## Package Layout

One flat Python package per bounded context at the project root — no `src/` wrapper:

```
catalog/          ← Python package (import as 'catalog')
  __init__.py
  domain/         ← entities, value objects, domain events, port interfaces (ABCs)
  application/    ← use cases; imports domain only, never infrastructure
  infrastructure/ ← FastAPI routers, SQLAlchemy models, RabbitMQ adapters
loans/            ← same structure; plural/singular naming matches the README section heading and this package's name everywhere
users/            ← same structure; any additional services follow the same pattern
```

The project root holds no business logic — it is orchestration and configuration only:
`pyproject.toml`, `.venv`, `features/`, `tests/`, `README.md`.

Dependency direction: `infrastructure` → `application` → `domain`. Never the reverse.
Domain classes are pure Python dataclasses — no SQLAlchemy or Pydantic imports allowed.

**The bounded contexts are independent services.** They share no code and no database; they
communicate only by publishing and consuming events. `import-linter` must enforce both halves of
that — the layering *within* each context and the isolation *between* them:

```toml
[[tool.importlinter.contracts]]
name = "catalog domain does not import application or infrastructure"
type = "forbidden"
source_modules = ["catalog.domain"]
forbidden_modules = ["catalog.application", "catalog.infrastructure"]

[[tool.importlinter.contracts]]
name = "catalog application does not import infrastructure"
type = "forbidden"
source_modules = ["catalog.application"]
forbidden_modules = ["catalog.infrastructure"]

# ...the same two for loans, plus both directions of context isolation:

[[tool.importlinter.contracts]]
name = "catalog does not import loans"
type = "forbidden"
source_modules = ["catalog"]
forbidden_modules = ["loans"]

[[tool.importlinter.contracts]]
name = "loans does not import catalog"
type = "forbidden"
source_modules = ["loans"]
forbidden_modules = ["catalog"]
```

Six contracts, not four. Without the last two nothing stops one context importing the other's
domain directly, which is the failure the whole architecture exists to prevent.

## Test Layout

All tests live under a single root `tests/` directory:

```
tests/
  conftest.py
  unit/
    catalog/
      domain/       ← unit tests for catalog domain (pure Python, no I/O)
      application/  ← unit tests for catalog application services (mocked ports)
      infrastructure/
    loans/
      domain/
      application/
      infrastructure/
  acceptance/
    conftest.py     ← Testcontainers session fixtures
    steps/
      catalog_steps.py   ← pytest-bdd step implementations for features/cat-*.feature
      loan_steps.py      ← pytest-bdd step implementations for features/loan-*.feature
  property/         ← Property-based tests (see /property-test-generator skill)
    catalog/
      domain/       ← hypothesis-based tests for domain invariants
      application/
      infrastructure/
    loans/
      domain/
      application/
      infrastructure/
features/           ← Gherkin specs (do not modify; owned by specifier)
```

## Testing Rules

- **Unit tests** (`tests/unit/`): pure Python, mock all ports (repositories, publishers), no I/O, no DB.
- **Acceptance tests** (`tests/acceptance/steps/`): pytest-bdd step implementations that execute the `.feature` files. Use Testcontainers for PostgreSQL and RabbitMQ — do NOT use in-memory SQLite for acceptance tests.
- **Acceptance step files must execute the feature files.** Each step file in `tests/acceptance/steps/` must call `scenarios("features/<file>.feature")` (or `@scenario(...)` per test function) so pytest actually runs the Gherkin scenarios as test cases. Step files without this call leave the feature files as dead documentation.
- **One PostgreSQL container per bounded context.** `tests/acceptance/conftest.py` provisions a
  separate session-scoped `PostgresContainer` for catalog and for loans — `catalog_postgres` and
  `loans_postgres`, not one shared `postgres_container` both engines connect to. The contexts are
  independent services that must be able to run against independent databases, and a single
  shared container means the suite never demonstrates that. Disjoint SQLAlchemy metadata is not
  a substitute: it hides a cross-context foreign key or a table-name collision instead of
  failing on it.
- **A cross-context event must be tested across the contexts, in one scenario.** At least one
  acceptance scenario has to publish from the producing context and observe the effect in the
  consuming one — for `BookReturned`: loans returns a book, and the catalog's stock goes up.
  Wire both apps to the *same* broker instance in the fixture so the event actually travels.
  - Testing the two halves separately does not count. A loans test that asserts against a
    recording publisher, plus a catalog test that hand-constructs a message and feeds the
    consumer directly, leaves the join between them — the part most likely to be wrong —
    untested by both.
  - An in-process broker is acceptable as the transport while no real adapter exists, provided
    it is shared across both apps. A publisher that accepts and drops the event is not: with a
    no-op publisher on one side, the scenario proves nothing about the path.
- **Prohibited patterns**:
  - Flat `tests/test_<story>.py` files (group by layer, not by story)
  - In-memory SQLite as a substitute for Testcontainers in acceptance tests
  - A step file with `@given`/`@when`/`@then` but no `scenarios(...)` / `@scenario(...)` call
  - One database container shared between both bounded contexts
  - A no-op publisher standing in for the producing half of a cross-context event test

## pyproject.toml Requirements

`requires-python` must be `">=3.10"`. Dev dependencies must include:

```
pytest-bdd>=7.0
testcontainers[postgres,rabbitmq]>=3.7
pytest-asyncio>=0.21
pytest-cov>=4.1
hypothesis>=6.0
cosmic-ray>=8.3
mypy>=1.5
ruff>=0.1
bandit[sarif]>=1.7
radon>=6.0
pip-audit>=2.7
```

## Runtime Prerequisites

`testcontainers` above is not a pure-Python dependency: it starts real PostgreSQL and RabbitMQ
containers, so `tests/acceptance/` needs a reachable container engine.

Probe with `docker info` before running that suite. If nothing answers, **skip the acceptance
tests and say so in the handoff** — do not run them anyway. Testcontainers waits on the daemon
indefinitely instead of failing, so the suite does not error out, it stops, and the worker is
eventually killed by its timeout having produced no diagnosis at all.

Unit tests have no such dependency and always run.

pytest config in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

## Local Run

To start each service locally for manual testing or development:

```bash
# Catalog service (default port 8000)
uvicorn catalog.infrastructure.api.main:app --reload

# Loans service (alternate port to avoid conflict)
uvicorn loans.infrastructure.api.main:app --reload --port 8001
```

## Quality Gates

Coverage, type checking, and lint are gated on every handoff, including the coder's. Mutation
testing is the architect's responsibility (full run, once per cycle); the refactorer only scans
mutation site counts, never runs the full suite (see `constitution/roles/coder.md` and
`refactorer.md` → Non-Ownership):

- Mutation score ≥ 80% on `domain/` and `application/`. `cosmic-ray`'s `module-path` is
  singular, so this is one session per service — `mutation-catalog.toml` and
  `mutation-loans.toml`, each excluding that service's `infrastructure`:

  ```toml
  [cosmic-ray]
  module-path = "catalog"
  timeout = 60.0
  excluded-modules = ["catalog/infrastructure/**/*.py"]
  # test-command must be `uv run python -m pytest`. Not a .venv path (absolute or
  # relative): it encodes one machine into a committed file. Not a bare `python`
  # either: that resolves to the host's default interpreter, which lacks the project
  # dependencies, so every mutant dies of the same collection error and the score
  # comes back a perfect 100%. `uv run` resolves the project environment from the
  # config's own directory and needs no shell state.
  test-command = "uv run python -m pytest tests/unit -x -q"

  [cosmic-ray.distributor]
  name = "local"
  ```

  ```bash
  uv run cosmic-ray init mutation-catalog.toml mutation-catalog.sqlite
  uv run cosmic-ray exec mutation-catalog.toml mutation-catalog.sqlite
  uv run cr-rate --fail-over 20 mutation-catalog.sqlite   # survival ≤ 20% == score ≥ 80%
  ```

  Record each run's score in `.mutation-scores.json` (see below). The gate is a floor, not a
  target: a score that falls while staying above 80% is a regression and must be reported.

  A per-cycle scoped config (`mutation-<work-item>-scoped.toml`) is encouraged and **must be
  committed** if its score is quoted in a handoff. A number nobody else can re-run is not
  evidence.

- Coverage ≥ 90%: `uv run python -m pytest --cov=catalog --cov=loans --cov-report=term-missing --cov-fail-under=90`
- Type checking: `uv run python -m mypy catalog/ loans/ --strict`  (also set `files = ["catalog", "loans"]` in `[tool.mypy]` so bare `mypy` works)
- Lint: `uv run python -m ruff check . && uv run python -m ruff format --check .`

  **`[tool.ruff.lint]` must set `select = ["E", "F", "I", "UP", "B"]`.** Ruff's default rule set
  is far narrower (`E4`, `E7`, `E9`, `F`) and omitting `select` silently drops import ordering,
  pyupgrade and bugbear — the gate still reports "All checks passed!" while checking much less.
  Do not narrow this list; if a specific rule is genuinely wrong for this project, disable that
  rule by name in `ignore` with a comment saying why.

- Docstring coverage ≥ 90%: `uv run interrogate catalog loans` with `fail-under = 90` in
  `[tool.interrogate]`.

- **Do not add entries to `[tool.coverage.run] omit`.** The acceptance suite runs against real
  containers and covers the infrastructure adapters, so they need no exemption; an `omit` entry
  removes a file from the denominator rather than testing it. If a module genuinely cannot be
  covered, say so in the handoff instead.

### `.mutation-scores.json`

Committed at the project root; the architect updates it every cycle:

```json
{
  "version": 1,
  "updated": "YYYY-MM-DD",
  "threshold": 80.0,
  "scores": {
    "catalog": {"last_score": 0.0, "mutants": 0, "survivors": 0,
                "known_equivalent": 0, "known_equivalent_detail": "", "last_run": ""},
    "loans":   {"last_score": 0.0, "mutants": 0, "survivors": 0,
                "known_equivalent": 0, "known_equivalent_detail": "", "last_run": ""}
  }
}
```

`known_equivalent` and its detail field are where equivalent mutants are accounted for — in the
record, not by editing the code they land on (see `engineering.md`, "A gate measures the code").
