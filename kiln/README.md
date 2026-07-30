# Kiln Framework Templates

Two buckets:

- `project/` — copied into every new project's `kiln/project/` during project init (`kiln.ps1 -Init` / `kiln.sh init`). Customize freely per project; your copy is what agents actually read at runtime.
- `framework/` — never copied anywhere. Referenced directly from this install by absolute path. Do not edit per-project; changes here affect every project using this framework install.
