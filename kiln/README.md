# Kiln Framework Templates

Two buckets:

- `project/` — copied into every new project's `kiln/project/` during project init (`bin/kiln.ps1 -Init` / `bin/kiln.sh init`). Customize freely per project; your copy is what agents actually read at runtime.
- `framework/` — never copied anywhere. Referenced directly from this install by absolute path. Holds the Python implementation (`launcher/`, `scheduler/`) as well as the templates and default profiles. Do not edit per-project; changes here affect every project using this framework install.
