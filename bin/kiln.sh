#!/usr/bin/env bash
# Kiln entry point (macOS/Linux) — a shim.
#
# All logic lives in Python under src/kiln/launcher/. This script only locates the
# framework, puts it on PYTHONPATH, and forwards its arguments through unchanged:
#
#   ./bin/kiln.sh /path/to/project
#   ./bin/kiln.sh init /path/to/project --example library-hub
#   ./bin/kiln.sh --stop
#   ./bin/kiln.sh --list-profiles
#
# The previous ~1370-line implementation was a permanently-lagging port of kiln.ps1; both
# are now the same Python, so Unix parity is structural rather than something to maintain.
# See full-python-plan.md.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
framework_root="$(dirname "$script_dir")"
package_root="$framework_root/src"

if [ ! -d "$package_root/kiln/launcher" ]; then
  echo "Error: Kiln launcher package not found at $package_root" >&2
  exit 1
fi

# Prefer a project-local virtualenv when present. The launcher and scheduler import only
# the standard library, so no install step is needed.
if [ -x "$framework_root/.venv/bin/python" ]; then
  python_bin="$framework_root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="python3"
elif command -v python >/dev/null 2>&1; then
  python_bin="python"
else
  echo "Error: python3 is required but was not found on PATH." >&2
  exit 1
fi

# A bare first argument that is not a known subcommand is treated as the project directory,
# preserving the old `kiln.sh /path/to/project` calling convention.
#
# Every subcommand kiln.launcher.infrastructure.cli recognises has to be listed here, or it gets swallowed as a
# directory: `send` and `inbox` were missing, so `kiln.sh send --to specifier ...` was
# rewritten to `--working-dir send --to specifier ...` and died on "unrecognized arguments:
# --to". Both are documented in the README and both were unusable on Unix; kiln.ps1 never had
# the bug because it forwards @args untouched.
args=("$@")
if [ "${#args[@]}" -gt 0 ]; then
  case "${args[0]}" in
    init|send|inbox|-*) ;;
    *)
      args=(--working-dir "${args[0]}" "${args[@]:1}")
      ;;
  esac
fi

export PYTHONPATH="$package_root${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m kiln.launcher.infrastructure.cli "${args[@]}"
