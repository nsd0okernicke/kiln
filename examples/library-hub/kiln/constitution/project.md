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
- **Always activate before any Python command**:
  - Windows: `<project_root>\.venv\Scripts\activate`
  - Unix: `source <project_root>/.venv/bin/activate`
- Install dependencies once after creation: `pip install -e ".[dev]"`
- **Do NOT create a new `.venv` inside your worktree.**

## Package Layout

Two flat Python packages at the project root — no `src/` wrapper:

```
catalog/          ← Python package (import as 'catalog')
  __init__.py
  domain/         ← entities, value objects, domain events, port interfaces (ABCs)
  application/    ← use cases; imports domain only, never infrastructure
  infrastructure/ ← FastAPI routers, SQLAlchemy models, RabbitMQ adapters
loan/             ← same structure
```

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
    loan/
      domain/
      application/
  acceptance/
    conftest.py     ← Testcontainers session fixtures
    steps/
      catalog_steps.py   ← pytest-bdd step implementations for features/cat-*.feature
      loan_steps.py      ← pytest-bdd step implementations for features/loan-*.feature
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
mutmut>=2.4
mypy>=1.5
ruff>=0.1
```

pytest config in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

## Quality Gates

Run before every handoff:

- Mutation score ≥ 80% on `domain/` and `application/`: `mutmut run --paths-to-mutate catalog/domain,catalog/application,loan/domain,loan/application`
- Coverage ≥ 90%: `pytest --cov=catalog --cov=loan --cov-report=term-missing`
- Type checking: `mypy catalog/ loan/ --strict`
- Lint: `ruff check . && ruff format --check .`
