# Framework Internals

Never copied into a project. Read directly from this install at generation/launch time. Editing these files changes behavior for every project using this framework install, immediately.

`launcher/` and `scheduler/` are the implementation — `bin/kiln.ps1` and `bin/kiln.sh` only put this directory on `PYTHONPATH` and call `python -m launcher.cli`. Both are covered by the pytest suite in `tests/`, which is the fastest way to see what any of it actually does.

`tools/` is a special case: not a per-project customization surface and never part of a project's persistent `kiln/` tree — but unlike the rest of this folder, its contents *are* copied, freshly, into the project's ephemeral `.kiln/tools/` on every launch (not once at scaffold time). Framework-owned either way; editing it here affects every project's next launch.
