# Project Rules — BattleZone

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

Throughout the rest of this document, **`python` means that interpreter** -- `python -m pytest`,
`python -m mypy`, `python -m ruff`. Tools with no module entry point are run from
`<project_root>\.venv\Scripts\` on Windows, `<project_root>/.venv/bin/` on Unix. A bare
`pytest` resolves against `PATH`, which without activation is the wrong interpreter or none at
all.

## The domain/application vs infrastructure Boundary

This is the single most important rule in this project. `battlezone/domain/` and
`battlezone/application/` must **never** `import pygame` (or anything from
`battlezone/infrastructure/`) — directly or transitively. All quality gates (coverage, mutation,
CRAP, strict mypy) apply only to these two packages; `battlezone/infrastructure/` is the
environment-bound rendering/input/window-management shell and is verified by manual playtest
instead (see `README.md` → Quality Gates).

Before handoff, a quick self-check: `grep -rn "import pygame" battlezone/domain battlezone/application`
must return nothing.

## Package Layout

Flat Python package at the project root — no `src/` wrapper:

```
battlezone/          ← Python package (import as 'battlezone')
  __init__.py
  domain/             ← pure simulation logic, zero pygame imports, zero I/O
    vector.py
    tank.py
    projectile.py
    obstacle.py
    arena.py
    collision.py
    ai.py
    projection.py
    scoring.py
  application/        ← orchestration; imports domain only, never infrastructure
    game_session.py    ← the one per-tick entry point: tick(dt, input_intents) -> FrameState
    frame_state.py       ← boundary DTO between application and infrastructure
    input_intent.py
  infrastructure/     ← pygame: window, input polling, wireframe drawing, main loop
    renderer.py
    input_adapter.py
    main.py
```

The project root holds no game logic — it is orchestration and configuration only:
`pyproject.toml`, `.venv`, `features/`, `tests/`, `README.md`. No `assets/` directory — wireframe
rendering only, no textures or sprites to manage.

Dependency direction: `infrastructure` → `application` → `domain`. Never the reverse.
Domain and application classes are pure Python dataclasses — no `pygame.Surface`, `pygame.Rect`,
or any other `pygame` type crosses into either package.

## Test Layout

All tests live under a single root `tests/` directory:

```
tests/
  conftest.py
  unit/
    domain/            ← unit tests for vector math, entities, collision, AI, projection, scoring
    application/         ← unit tests for GameSession.tick orchestration
  acceptance/
    conftest.py           ← fixtures for a fresh GameSession per scenario
    steps/
      tank_steps.py          ← pytest-bdd step implementations for features/tank-*.feature
      ai_steps.py               ← pytest-bdd step implementations for features/ai-*.feature
      game_steps.py                ← pytest-bdd step implementations for features/game-*.feature
  property/               ← Property-based tests (see /property-test-generator skill)
    domain/                  ← Hypothesis tests for domain invariants (projection, collision, physics)
features/                     ← Gherkin specs (do not modify; owned by specifier)
```

## Testing Rules

- **Unit tests** (`tests/unit/`): pure Python, no I/O, no pygame import anywhere in `domain/` or `application/` tests either — if a test needs `pygame`, it belongs in a manual playtest note, not this tree.
- **Acceptance tests** (`tests/acceptance/steps/`): pytest-bdd step implementations that drive `GameSession` directly and assert on `FrameState`/game state — fully headless, no window ever opens, no `SDL_VIDEODRIVER` workaround needed because this layer never touches pygame.
- **Acceptance step files must execute the feature files.** Each step file in `tests/acceptance/steps/` must call `scenarios("features/<file>.feature")` (or `@scenario(...)` per test function) so pytest actually runs the Gherkin scenarios as test cases. Step files without this call leave the feature files as dead documentation.
- **Prohibited patterns**:
  - Any `import pygame` inside `battlezone/domain/`, `battlezone/application/`, or their test trees
  - Flat `tests/test_<story>.py` files (group by layer, not by story)
  - A step file with `@given`/`@when`/`@then` but no `scenarios(...)` / `@scenario(...)` call

## pyproject.toml Requirements

`requires-python` must be `">=3.10"`. Dependencies must include:

```
pygame>=2.5
```

Dev dependencies must include:

```
pytest-bdd>=7.0
hypothesis>=6.0
mutmut>=2.4
mypy>=1.5
ruff>=0.1
```

pytest config in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

## Local Run

```bash
python -m battlezone.infrastructure.main
```

Controls: arrow keys or WASD to move/turn, space to fire, Esc to quit. This is a manual step —
there is no automated way to verify rendering/input behavior; see Quality Gates in `README.md`.

## Quality Gates

Coverage, type checking, and lint are gated on every handoff, including the coder's. Mutation
testing is the architect's responsibility (full run, once per cycle); the refactorer only scans
mutation site counts, never runs the full suite (see `constitution/roles/coder.md` and
`refactorer.md` → Non-Ownership). Scoped to `domain/` and `application/` only:

- Mutation score ≥ 80%: `mutmut run --paths-to-mutate battlezone/domain,battlezone/application`
- Coverage ≥ 90%: `python -m coverage run -m pytest tests/unit tests/property && python -m coverage report`
- Type checking: `python -m mypy battlezone/domain battlezone/application --strict`
- Lint (whole package, including `infrastructure/`): `python -m ruff check . && python -m ruff format --check .`
- Manual playtest whenever `infrastructure/` changes (see Local Run) — no automated substitute
