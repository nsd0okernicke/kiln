# Kiln Architecture Review and Improvement Plan

Review date: 2026-08-22

## Summary

Kiln is a pragmatic, well-tested Python process orchestrator. Its major responsibilities are
reasonably separated: `launcher` prepares projects and processes, `scheduler` runs the work
cycle, `cockpit` and `dashboard` expose state, `proxy` captures traffic, and adapters isolate
agent and terminal integrations.

The project generally follows modern Python practices. It uses dataclasses, `pathlib`, context
managers, type annotations, standard logging, explicit CLI entry points, and extensive tests.
Ruff passes, all framework Python files compile, and pytest collects 1,762 tests across 35 test
modules. A full test run exceeded the available 125-second window and ended with a Python 3.14
stdout flush error, so this review does not claim a complete passing run.

Strict hexagonal architecture would not make sense across the entire project. Much of Kiln is
deliberately infrastructure code that controls Git, subprocesses, files, terminals, SQLite, and
local HTTP. However, a hexagonal scheduler core would improve maintainability. Kiln already
uses this style locally through injected scheduler effects, backend adapters, terminal adapters,
and pure state builders.

No critical architectural defect was found. The main risks are maintainability, weakly typed
cross-module contracts, nonstandard packaging, and unclear runtime-state durability.

## Findings

### 1. Scheduler responsibilities are too concentrated — medium priority

`scheduler/role_scheduler.py` is roughly 1,384 lines and contains cycle policy, retry and budget
logic, work-item identity, queue access, Git coordination, debug output, adapter selection,
argument parsing, and the process loop. `SchedulerContext` injects some effects, but the core
still calls concrete `db` and `git_ops` modules directly.

Recommended direction:

- Preserve `run_once()` as the central application use case.
- Introduce small `Protocol` ports for `MessageQueue`, `Worktree`, and `WorkerRunner`.
- Move retry, budget, and escalation decisions into pure policy functions.
- Wrap the existing SQLite and Git functions instead of building a large service hierarchy.

### 2. Queue records and lifecycle rules are weakly typed — medium priority

`scheduler/db.py` centralizes SQL well, but returns raw dictionaries, represents states as plain
strings, and exposes timestamp strings with mixed UTC/local conventions. Multiple consumers must
know the row shape and lifecycle rules.

Recommended direction:

- Add a `MessageStatus` `StrEnum`.
- Represent queue records with frozen dataclasses or `TypedDict`.
- Centralize and enforce allowed lifecycle transitions.
- Use timezone-aware UTC internally and localize only in presentation code.
- Document the single-consumer-per-role assumption. If it changes, claim messages atomically
  with a write transaction and `UPDATE ... RETURNING`.

### 3. Python packaging and imports are nonstandard — medium priority

The root `pyproject.toml` is only tool configuration. The shims put `kiln/framework` on
`PYTHONPATH`, exposing packages such as `launcher` and `scheduler` at top level. Some entry
modules modify `sys.path`, and `mcp-server` is not an importable package name.

This works, but weakens IDE discovery, dependency management, editable installation, and static
analysis.

Recommended direction:

- Migrate toward an installable `src/kiln/` package.
- Use namespaces such as `kiln.launcher` and `kiln.scheduler`.
- Add console entry points for `kiln` and supporting processes.
- Keep existing scripts as compatibility wrappers during migration.
- Rename `mcp-server` to an importable module name.

### 4. Static typing is descriptive but not enforced — medium priority

Annotations are common, but there is no mypy or pyright configuration. Important boundaries
still use raw `dict` and `Callable[..., ...]`, so annotations do not fully protect cross-module
contracts.

Recommended direction:

- Add pyright or mypy in gradual mode and run it in CI.
- Begin with pure modules, configuration values, command builders, and queue records.
- Do not require strict typing of subprocess and HTTP internals in the first pass.

### 5. Several modules have large change surfaces — low priority

Besides `role_scheduler.py`, large modules include `workspace.py`, `cli.py`, `proxy/capture.py`,
`dashboard.py`, `config.py`, `db.py`, and `proxy/server.py`. Size alone is not a defect, but each
contains multiple independently changing concepts.

Useful seams when related work next touches them:

- `launcher/cli.py`: dispatch, launch planning, and proxy lifecycle.
- `launcher/workspace.py`: repository setup, worktrees, agent configuration, and skills.
- `scheduler/dashboard.py`: snapshot collection, metrics, and terminal rendering.
- `proxy/capture.py`: provider parsing and traffic persistence.
- `scheduler/db.py`: schema/migrations, queue commands, and reporting queries.

Avoid a mechanical file split; extract only around a real responsibility boundary.

### 6. Backend adapters repeat process lifecycle code — low priority

The four agent adapters correctly isolate backend-specific commands and event formats, but each
contains similar subprocess, stream, timeout, and watchdog flow. Shared process termination and
watchdog behavior already exist.

Recommended direction: extract a small shared process-stream runner only after verifying that all
four adapters need the same lifecycle guarantees. Keep command construction and parsing separate.
Prefer composition over an adapter base-class hierarchy.

### 7. Dependency and CI metadata are incomplete — low priority

Runtime and development dependencies are not declared in one project definition, and no
repository CI workflow is present under `.github/workflows`.

Recommended direction:

- Declare supported Python versions and dependency groups.
- Add CI for Ruff, tests, compilation, and gradual type checking on supported operating systems.
- Keep authenticated live-agent validation separate from deterministic CI.

### 8. Runtime-state durability is undecided — medium priority

The SQLite queue is described as disposable and has no ordered migrations, while retries,
dashboards, cost tracking, and work-item history increasingly make its contents valuable.
In-memory spend tracking also resets when a scheduler restarts.

Choose and document one policy:

- **Ephemeral coordination:** retain deletion-based upgrades and explicitly describe metrics and
  history as session-local; or
- **Durable execution history:** add schema migrations, persisted budgets, retention,
  backup/export, and stronger transactional guarantees.

Both are valid. Remaining between them will create subtle reliability problems as features grow.

## Python and OOP assessment

Kiln should not add classes merely to appear more object-oriented. Functions and dataclasses are
idiomatic for value transformation, command construction, and orchestration. The project already
uses composition more often than inheritance, keeps value objects small, and isolates backend
behavior.

The main OOP issue is not too few classes; it is that a few application services know too many
concrete collaborators. Small protocol-based ports would improve dependency inversion without
introducing class-heavy ceremony.

## Appropriate target architecture

The recommended target is a hexagonal scheduler core inside a pragmatic modular monolith:

```text
                    CLI / cockpit / MCP
                           |
                    application use cases
                  (launch, run cycle, retry)
                           |
          +----------------+----------------+
          |                |                |
     MessageQueue       Worktree        WorkerRunner
          |                |                |
       SQLite             Git       Claude/Codex/etc.
```

The launcher remains the composition root. Terminal, filesystem, HTTP, and proxy modules remain
explicit infrastructure code; they do not need artificial domain abstractions.

## Execution plan

1. Add gradual static type checking and establish a clean baseline for pure modules.
2. Introduce typed queue records and message states without changing SQLite behavior.
3. Add scheduler ports for queue, Git worktree operations, and worker execution.
4. Extract retry, budget, and escalation policies from `role_scheduler.py` with characterization
   tests protecting current behavior.
5. Decide whether `.kiln` data is ephemeral or durable; implement and document that decision.
6. Split oversized modules opportunistically along the responsibility seams above.
7. Consolidate adapter process execution where behavior is genuinely common.
8. Add standard package metadata, dependency groups, and console entry points while preserving
   the existing shims.
9. Add a cross-platform CI matrix for linting, compilation, tests, and type checking.

Each step should be independently reviewable and behavior-preserving. Do not begin with a broad
directory rewrite or a repository-wide ports-and-adapters conversion.
