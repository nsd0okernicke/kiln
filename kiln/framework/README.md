# Framework Internals

Never copied into a project. Read directly from this install at generation/launch time (see kiln.ps1 / kiln.sh). Editing these files changes behavior for every project using this framework install, immediately.

`tools/` is a special case: not a per-project customization surface and never part of a project's persistent `kiln/` tree — but unlike the rest of this folder, its contents *are* copied, freshly, into the project's ephemeral `.kiln/tools/` on every launch (not once at scaffold time). Framework-owned either way; editing it here affects every project's next launch.
