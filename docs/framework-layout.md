# Framework Internals

Never copied into a project. Read directly from this install at generation/launch time. Editing these files changes behavior for every project using this framework install, immediately.

`launcher/` and `scheduler/` are the implementation — `bin/kiln.ps1` and `bin/kiln.sh` only put this directory on `PYTHONPATH` and call `python -m kiln.launcher.cli`. Both are covered by the pytest suite in `tests/`, which is the fastest way to see what any of it actually does.

`tools/` is a special case: not a per-project customization surface and never part of a project's persistent `kiln/` tree — but unlike the rest of this folder, its contents *are* copied, freshly, into the project's ephemeral `.kiln/tools/` on every launch (not once at scaffold time). Framework-owned either way; editing it here affects every project's next launch.

## Runtime-state policy

Kiln deliberately treats `.kiln/` as **ephemeral coordination state**, not durable execution history. The SQLite queue, status snapshots, logs, session records, and in-memory scheduler budgets belong to one local swarm run. They may be deleted and recreated when a schema changes; they are not a backup, audit log, or source for resuming a project after `.kiln/` is removed. Git commits and handoff summaries are the durable record.

One scheduler process consumes messages for each `(role, branch)` pair. Startup recovery relies on that invariant when it returns rows left in `processing` to `delivered`; running two consumers for the same pair is unsupported. Supporting parallel consumers or durable history later requires ordered migrations, transactional message claiming, persisted budgets, retention, and export/backup as one coherent change.
