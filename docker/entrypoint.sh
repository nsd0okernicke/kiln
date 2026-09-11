#!/bin/sh
# Phase 3: Kiln container entrypoint.
#
# Mounts:
#   /seed  — host repository, read-only
#   /run   — run directory, read-write (shared with host at .kiln/runs/<id>/)
#
# Env:
#   KILN_REF        — branch or tag to check out (default: main)
#   KILN_PROFILE    — profile name (default: full)
#   KILN_RUN_ID     — run identifier (default: derived from branch)
#
# The entrypoint:
#   1. Clones the seed repo into /run/workspace
#   2. Checks out the target ref
#   3. Pip-installs Kiln from the workspace
#   4. Launches the swarm headless

set -e

: "${KILN_REF:=main}"
: "${KILN_PROFILE:=full}"
: "${KILN_RUN_ID:=}"

echo "[kiln-container] cloning seed repo into /run/workspace..."
git clone /seed /run/workspace
cd /run/workspace

echo "[kiln-container] checking out ref: $KILN_REF"
git checkout "$KILN_REF" 2>/dev/null || git checkout -b "$KILN_REF"

echo "[kiln-container] installing Kiln framework..."
pip install -e /run/workspace --no-cache-dir 2>&1 | tail -1

# Build the run flags.
RUN_FLAGS="--working-dir /run/workspace --terminal headless --profile $KILN_PROFILE"
if [ -n "$KILN_RUN_ID" ]; then
    RUN_FLAGS="$RUN_FLAGS --run $KILN_RUN_ID"
fi

# If credentials were seeded into /run/credentials/, copy them to the expected locations.
if [ -d /run/credentials ]; then
    echo "[kiln-container] seeding credentials..."
    for cred_dir in /run/credentials/*/; do
        [ -d "$cred_dir" ] || continue
        target="$HOME/.${cred_dir##*/}"
        mkdir -p "$target"
        cp -r "$cred_dir"* "$target/"
    done
fi

echo "[kiln-container] launching swarm..."
echo "  kiln $RUN_FLAGS"
exec python -m kiln.launcher.infrastructure.cli $RUN_FLAGS
