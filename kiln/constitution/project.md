# Project Rules

- This project is configured for Kiln with five agents: specifier, coder, refactorer, architect, and selftest.
- Project language: Python.
- Preserve project-local Kiln configuration under `kiln/`.
- Keep swarm state local under `.ksiln/` (SQLite message queue) and worktrees under `.worktrees/`.
- Prefer terse, explicit handoffs that report state and request role-appropriate review. Do not include verifications or sender process narrative.
- Do not change another role's prompt or workflow ownership without explicit user direction.

---

## Architecture & Layering Rules

### 3-Layer Structure
The codebase is organized into three layers with unidirectional dependencies (always flowing from outside to inside):

1. **Infrastructure** (outermost, knows everything)
   - FastAPI REST API, SQLAlchemy ORM, RabbitMQ adapters, Pydantic schemas
   - Responsibility: HTTP bindings, database adapters, message queue handlers, dependency injection
   - Location: `infrastructure/` package

2. **Application** (middle, orchestrates domain via ports)
   - Use cases, application services
   - Knows `domain/`, uses `domain/ports/`, does NOT import from `infrastructure/`
   - Location: `application/` package

3. **Domain** (innermost, pure business logic)
   - Entities, Value Objects, Domain Events, Port Interfaces (ABCs)
   - Zero dependencies on `application/` or `infrastructure/`
   - Location: `domain/` package

### Dependency Rules (Enforced)
| From | To | Allowed? |
|------|----|----|
| `infrastructure/` | `application/` | ✅ Yes |
| `infrastructure/` | `domain/` | ✅ Yes |
| `application/` | `domain/` | ✅ Yes |
| `application/` | `infrastructure/` | ❌ **No** (only via ports) |
| `domain/` | `application/` | ❌ **No** |
| `domain/` | `infrastructure/` | ❌ **No** |

Violations detected by static analysis tools (mypy) or code review must be fixed before merge.

### Mapping Pattern
Clean layer boundaries require adapters at the infrastructure layer:
- **HTTP Request** (Pydantic DTO) → mapped by `infrastructure/api/` → **Domain Object** → **Use Case**
- **Domain Object** → mapped by `infrastructure/db/` → **ORM Model** → **Database**
- **Database** → **ORM Model** → mapped by `infrastructure/db/` → **Domain Object** → returned via API

Domain classes are **pure Python dataclasses** with no ORM (SQLAlchemy) or schema (Pydantic) imports.

### Package Structure (Per Service)
```
<service>/
├── features/                    (Gherkin acceptance specifications, specifier role)
│   ├── user_registration.feature
│   ├── api/
│   │   └── auth.feature
│   └── ...
│
├── src/<service_name>/
│   ├── domain/
│   │   ├── <entity>.py              (pure dataclasses, business logic)
│   │   ├── events/                  (domain events as dataclasses)
│   │   └── ports/
│   │       ├── <x>_repository.py    (ABC interfaces, no implementation)
│   │       └── message_publisher.py (ABC interface)
│   │
│   ├── application/
│   │   ├── <use_case>.py            (orchestrates domain, calls ports)
│   │   └── <service>.py             (application service, if needed)
│   │
│   └── infrastructure/
│       ├── api/
│       │   ├── schemas/             (Pydantic DTOs)
│       │   └── routers/             (FastAPI endpoints)
│       ├── db/
│       │   ├── models.py            (SQLAlchemy ORM models)
│       │   └── <x>_repository.py    (port implementations)
│       ├── messaging/
│       │   ├── publisher.py         (RabbitMQ publisher adapter)
│       │   └── consumer.py          (RabbitMQ consumer adapter)
│       └── config/
│           └── settings.py          (pydantic-settings)
```

---

## Tech Stack (Locked Decisions)

- **Language**: Python 3.10+ with async/await patterns
- **REST Framework**: FastAPI (async, modern)
- **ORM**: SQLAlchemy 2.0+ (async driver: asyncpg for PostgreSQL)
- **Data Validation**: Pydantic v2 (schemas, DTOs, settings)
- **Database**: PostgreSQL (two isolated databases: catalog_db, loan_db)
- **Message Queue**: RabbitMQ (async, event-driven, Topic exchange pattern)
- **Testing**: pytest with async support
- **Integration Tests**: Testcontainers (PostgreSQL, RabbitMQ containers)
- **Quality Tools**: 
  - `mutmut` (mutation testing)
  - `mypy` (type checking, strict mode)
  - `ruff` (linting)
  - `black` (code formatting)
  - `radon` (complexity/CRAP)
- **Package Manager**: `uv` (Python environment management)

All services use the same tech stack. No divergence.

---

## Quality Gates

- **Mutation Testing**: `domain/` and `application/` packages must achieve **mutation score ≥ 80%**
  - Run after each layer completion: `mutmut run --paths domain,application`
  - Surviving mutants indicate weak or missing test assertions
  
- **Test Coverage**: All code must achieve **> 90% coverage**
  - Check with: `coverage run -m pytest && coverage report`
  
- **Type Checking**: All code must pass `mypy` in strict mode
  - No `type: ignore` comments without inline explanation
  - Run: `mypy src/`
  
- **Code Style**: All code must pass `ruff` and `black`
  - Run: `ruff check . && black --check .`
  - Auto-fix: `black .` and `ruff check --fix .`

Do not merge code that fails any gate. All gates are checked before handoff.

---

## Testing Strategy

### Three Test Levels

**1. Unit Tests** (domain/ and application/ layers)
- Test pure business logic in isolation
- Mock all dependencies (repositories, message publishers)
- Run fast: `pytest -m 'not integration'` (under 10 seconds)
- Coverage: > 90%
- Mutation score: ≥ 80%

**2. Acceptance Tests** (infrastructure/ layer)
- Test complete user journeys against real services
- Use Testcontainers to spin up PostgreSQL and RabbitMQ
- Run slower: `pytest` (includes all integration tests, ~30-60 seconds)
- Verify API contracts and cross-service messaging
- Examples:
  - User creates an account, borrows a book, receives PENDING
  - Catalog publishes BookReserved; Loan Service receives it and updates status to ACTIVE
  - User returns book; BookReturned event published; Catalog stock increases

**3. Manual/Observer Tests** (integration endpoints for event consumption)
- RabbitMQ message consumers should be tested via acceptance tests
- Spin up containers, publish an event, verify state change

### Test Organization
```
<service>/src/<service_name>/
├── domain/
│   └── tests/           (unit tests for domain entities/events)
├── application/
│   └── tests/           (unit tests for use cases)
└── infrastructure/
    ├── api/
    │   └── tests/       (acceptance tests for API endpoints)
    ├── db/
    │   └── tests/       (acceptance tests for repositories)
    └── messaging/
        └── tests/       (acceptance tests for publishers/consumers)
```

### Running Tests

| Command | What | When |
|---------|------|------|
| `pytest` | All tests (unit + acceptance) | Before handoff, final verification |
| `pytest -m 'not integration'` | Unit tests only | Quick feedback during development |
| `pytest infrastructure/api/tests/` | API acceptance tests only | Focused on API layer changes |
| `pytest --cov=src/` | Coverage report | Before mutation testing |
| `mutmut run --paths domain,application` | Mutation testing | After each layer completion |

---

## Non-Functional Requirements

- **Code Language**: All source code (comments, docstrings, variable names, function names, error messages) must be **English only**. No German, no French, no other languages except English.
  - Test data (e.g., user names in seeds) may contain Unicode characters
  
- **Error Handling**: Return appropriate HTTP status codes with descriptive error messages
  - 404 for not found, 409 for conflict, 422 for validation errors, 500 for server errors
  
- **Logging**: Structured JSON logging in the infrastructure layer
  - Use standard library `logging` with JSON formatters
  - Log all significant state transitions and errors
  
- **Authentication**: Not required for MVP
  - Optional: JWT support can be added in a future phase
