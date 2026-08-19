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
- **Prohibited patterns**:
  - Flat `tests/test_<story>.py` files (group by layer, not by story)
  - In-memory SQLite as a substitute for Testcontainers in acceptance tests
  - A step file with `@given`/`@when`/`@then` but no `scenarios(...)` / `@scenario(...)` call

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
  excluded-modules = ["catalog/infrastructure/*"]
  test-command = "python -m pytest tests/unit -x -q"

  [cosmic-ray.distributor]
  name = "local"
  ```

  ```bash
  .venv\Scripts\cosmic-ray init mutation-catalog.toml mutation-catalog.sqlite
  .venv\Scripts\cosmic-ray exec mutation-catalog.toml mutation-catalog.sqlite
  .venv\Scripts\cr-rate --fail-over 20 mutation-catalog.sqlite   # survival ≤ 20% == score ≥ 80%
  ```

- Coverage ≥ 90%: `python -m pytest --cov=catalog --cov=loans --cov-report=term-missing`
- Type checking: `python -m mypy catalog/ loans/ --strict`
- Lint: `python -m ruff check . && python -m ruff format --check .`
