# Kiln Project Scaffolding

`project/` is copied into every new project's `kiln/project/` during project init
(`bin/kiln.ps1 -Init` / `bin/kiln.sh init`). Customize the generated copy freely; it is
what agents read at runtime. Kiln's implementation and framework-owned resources live under
the repository's standard `src/kiln/` package.
