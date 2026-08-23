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

One scheduler process consumes messages for each `(role, branch)` pair. Startup recovery relies on that invariant when it returns rows left in `processing` to `delivered`; running two consumers for the same pair is unsupported. Supporting parallel consumers or durable history later requires ordered migrations, transactional message claiming, persisted budgets, retention, and export/backup as one coherent change.
