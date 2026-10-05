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
#   3. Sets PYTHONPATH for the Kiln framework
#   4. Seeds agent config files (JSON only, no sessions/bin)
#   5. Launches the swarm headless

set -e

: "${KILN_REF:=main}"
: "${KILN_PROFILE:=full}"
: "${KILN_RUN_ID:=}"

echo "[kiln-container] cloning seed repo into /run/workspace..."
git clone /seed /run/workspace
cd /run/workspace

echo "[kiln-container] checking out ref: $KILN_REF"
git checkout "$KILN_REF" 2>/dev/null || git checkout -b "$KILN_REF"

echo "[kiln-container] setting PYTHONPATH to /run/workspace/src..."
export PYTHONPATH=/run/workspace/src

# Build the run flags.
RUN_FLAGS="--working-dir /run/workspace --terminal headless --profile $KILN_PROFILE"
if [ -n "$KILN_RUN_ID" ]; then
    RUN_FLAGS="$RUN_FLAGS --run $KILN_RUN_ID"
fi

# Seed only the essential agent config files (JSON, no binaries/sessions/npm).
if [ -d /run/credentials ]; then
    echo "[kiln-container] seeding credentials..."
    for cred_dir in /run/credentials/*/; do
        [ -d "$cred_dir" ] || continue
        label="${cred_dir%%/}"
        label="${label##*/}"
        target="$HOME/.$label/agent"
        mkdir -p "$target"
        if [ -d "$cred_dir/agent" ]; then
            for f in "$cred_dir"agent/*.json; do
                # Skip settings and cache files that trigger extension installs.
                name="${f##*/}"
                case "$name" in
                    auth.json|models.json|trust.json)
                        [ -f "$f" ] && cp "$f" "$target/"
                        ;;
                esac
            done
        else
            for f in "$cred_dir"*.json; do
                [ -f "$f" ] && cp "$f" "$target/"
            done
        fi
    done
fi

echo "[kiln-container] launching swarm..."
echo "  kiln $RUN_FLAGS"

# Write a small Python bootstrap that sets sys.path before importing kiln.
cat > /tmp/launch.py << 'PYEOF'
import sys
sys.path.insert(0, '/kiln/src')
sys.path.insert(0, '/run/workspace/src')
from kiln.launcher.infrastructure.cli import main
sys.exit(main())
PYEOF

# Run the launcher (do NOT exec — we need the shell to stay as PID 1).
# The launcher spawns detached children; if PID 1 exits, Docker kills them all.
set +e  # don't exit on launcher failure
python /tmp/launch.py $RUN_FLAGS
LAUNCHER_EXIT=$?
echo "[kiln-container] launcher exited (rc=$LAUNCHER_EXIT); keeping container alive for swarm processes..."

# Keep the container alive forever.  ``tail -f /dev/null`` is more portable than
# ``sleep infinity`` and never returns.  When ``docker stop`` sends SIGTERM this
# exits and the container stops cleanly.
tail -f /dev/null
