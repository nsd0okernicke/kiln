# Framework Internals

Never copied into a project. Read directly from this install at generation/launch time. Editing these files changes behavior for every project using this framework install, immediately.

The installable application lives entirely under `src/kiln/`. `bin/kiln.ps1` and
`bin/kiln.sh` put `src/` on `PYTHONPATH` and call
`python -m kiln.launcher.infrastructure.cli`.

The main feature packages are organized vertically, with hexagonal layers inside each feature:

```text
src/kiln/
├── launcher/                # Profiles, generation, workspaces and terminal launch
│   ├── domain/
│   ├── application/
│   └── infrastructure/
├── scheduler/               # Deterministic message-to-worker cycle
│   ├── domain/
│   ├── application/         # Use cases and ports
│   └── infrastructure/      # Agent, database, Git, CLI and terminal adapters
├── cockpit/                 # Browser operations and swarm-state projection
│   ├── application/
│   └── infrastructure/
├── knowledge/               # Curated project knowledge: catalog, extraction and search
│   ├── domain/              # Catalog rules and text chunking
│   ├── application/         # Use cases and ports
│   └── infrastructure/      # JSON catalog, file extraction, SQLite FTS index and CLI
├── proxy/                   # Capture policy, HTTP forwarding and persistence
│   ├── domain/
│   └── infrastructure/
├── mcp_server/              # Small MCP transport package; no business domain of its own
└── resources/               # Packaged templates, tools, profiles and project scaffold
```

Dependencies point inward: domain code does not import infrastructure, and application code
expresses external needs through ports. Infrastructure owns concrete SQLite, Git, HTTP, terminal
and agent-CLI integrations. A layer is not created when a package has no corresponding concern;
`mcp_server`, for example, remains a small transport boundary rather than gaining empty folders.

Application modules use names that describe their responsibility. Scheduler use cases therefore
live directly in `scheduler/application/process_next_message.py` and
`recover_interrupted_work.py`, while launcher artifact generation lives in
`launcher/application/artifacts.py`. Scheduler's several outbound protocols justify the
`scheduler/application/ports/` package; cockpit's single contract remains
`cockpit/application/ports.py`. The launcher's concrete socket probe is infrastructure and lives
in `launcher/infrastructure/networking.py`, not in an application port module.

Knowledge names its three outbound needs separately in `knowledge/application/ports.py` --
the committed catalog, the local documents and the disposable index -- because they have
different lifetimes and are independently substitutable. `knowledge/infrastructure/factory.py`
is the one module that knows both concrete locations, `kiln/project/knowledge.json` and
`.kiln/knowledge.db`.

Tests mirror the package beneath their test type:

```text
tests/
├── unit/kiln/               # Fixed examples and regressions
├── property/kiln/           # Hypothesis invariants (`test_*_properties.py`)
├── integration/kiln/        # Deterministic local infrastructure boundaries
└── mutation/                # Cosmic Ray tier configuration
```

`src/kiln/resources/tools/` is a special case: it is not a per-project customization
surface, but its contents are copied freshly into `.kiln/tools/` on every launch.
`src/kiln/resources/project/` is the scaffold source copied to the generated project's editable
`kiln/project/`; it is framework data, not a second implementation tree.

## Runtime-state policy

Kiln deliberately treats `.kiln/` as **ephemeral coordination state**, not durable execution history. The SQLite queue, status snapshots, logs, session records, and in-memory scheduler budgets belong to one local swarm run. They may be deleted and recreated when a schema changes; they are not a backup, audit log, or source for resuming a project after `.kiln/` is removed. Git commits and handoff summaries are the durable record.

One scheduler process consumes messages for each `(run, role, branch)` triple. Startup recovery relies on that invariant when it returns rows left in `processing` to `delivered`; running two consumers for the same triple is unsupported. Supporting parallel consumers or durable history later requires ordered migrations, transactional message claiming, persisted budgets, retention, and export/backup as one coherent change.

### Runs

A **run** is a named swarm instance created with ``kiln --run <name>``. Each named run gets its own full repository checkout at ``.worktrees/<run_id>/`` (via ``git clone --local``), with an index entry at ``.kiln/runs/<run_id>/run.json`` in the original project root. The checkout has its own ``.kiln/``, its own role worktrees, and its own branches -- no path collisions with other runs.

``KILN_STATE_DIR`` (``.kiln/``) is exported by every terminal backend and is the preferred way for ``set-status.py`` and other tools to locate runtime state, replacing the WezTerm-only ``KILN_PROJECT_DIR``.

``.kiln/runs/`` is the discovery index: each directory there is a run, and ``run.json`` holds identity, checkout path, and executor metadata. Use ``kiln runs`` to list, and ``kiln runs rm <id>`` to remove a run and its checkout.

```text
.kiln/
  runs/                        # Run index (discovery only)
    exp-auth/
      run.json                 # Immutable identity record
      state.json               # Mutable liveness indicator
    feature-auth/
    ...
.worktrees/                    # Per-run checkouts (git clone --local)
  exp-auth/
    .git/                      # Real git directory, NOT a linked worktree
    .kiln/                     # This run's full state: messages.db, status/, logs/...
    .worktrees/coder/          # Role worktrees (standard per-swarm layout)
    .worktrees/reviewer/
    ...
```
