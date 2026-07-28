#!/usr/bin/env zsh
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: swarm-cleanup.sh <tmux-socket> <window-ids-file> [session ...]" >&2
  exit 1
fi

TMUX_SOCKET="$1"
WINDOW_IDS_FILE="$2"
TERMINAL_BACKEND="${Kiln_TERMINAL_BACKEND:-terminal-app}"
WORKING_DIR="$(cd "$(dirname "$WINDOW_IDS_FILE")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
shift
shift

has_command() {
  command -v "$1" &>/dev/null
}

source "$SCRIPT_DIR/terminal-adapter.sh"
load_terminal_backend "$TERMINAL_BACKEND"

echo "Cleaning up Kiln sessions in $WORKING_DIR"

# Step 1: Close terminal sessions
echo ""
echo "Step 1: Closing terminal sessions..."
for session in "$@"; do
  tmux -S "$TMUX_SOCKET" kill-session -t "$session" 2>/dev/null || true
done

sleep 1

if [[ -f "$WINDOW_IDS_FILE" ]]; then
  while IFS= read -r window_id; do
    [[ -n "$window_id" ]] || continue
    terminal_close_window "$window_id"
  done < "$WINDOW_IDS_FILE"
fi

# Step 2: Preserve git branches (audit trail across multiple cycles), prune worktree refs.
# Mirrors kiln-cleanup.ps1 Steps 2-6 — kept in sync with whatever kiln.sh actually
# generates (see write_worker_agent_file / ensure_initial_gitignore / the .mcp.json
# writers in kiln.sh). kiln.sh has no CLAUDE.md/copilot-instructions.md or skills-dir
# equivalent (it uses --append-system-prompt-file from .kiln/prompts/, and doesn't
# copy skills), so there's nothing to remove for those here.
echo ""
echo "Step 2: Preserving sub-branches as audit trail..."
SUB_BRANCHES_FILE="$WORKING_DIR/.git/kiln-sub-branches"
if [[ -f "$SUB_BRANCHES_FILE" ]]; then
  preserved_branches=()
  while IFS= read -r branch; do
    [[ -n "$branch" ]] || continue
    preserved_branches+=("$branch")
  done < "$SUB_BRANCHES_FILE"

  if [[ ${#preserved_branches[@]} -gt 0 ]]; then
    echo "  Sub-branches preserved (not deleted):"
    for branch in "${preserved_branches[@]}"; do
      echo "     - $branch"
    done
    echo ""
    echo "  These branches are the audit trail for agent work across multiple cycles."
    echo "  To delete them, run: git branch -D ${preserved_branches[*]}"
  fi
  rm -f "$SUB_BRANCHES_FILE"
fi

git -C "$WORKING_DIR" worktree prune 2>/dev/null || true
echo "  Pruned stale worktree references"

# Step 3: Remove worktree directories
echo ""
echo "Step 3: Removing worktree directories..."
WORKTREES_DIR="$WORKING_DIR/.worktrees"
if [[ -d "$WORKTREES_DIR" ]]; then
  rm -rf "$WORKTREES_DIR"
  echo "  Removed .worktrees/ directory"
fi

# Step 4: Remove generated instruction/config files from main working directory
echo ""
echo "Step 4: Removing generated instruction files..."

if [[ -f "$WORKING_DIR/.mcp.json" ]]; then
  rm -f "$WORKING_DIR/.mcp.json"
  echo "  Removed .mcp.json"
fi

# Worker agent files (write_worker_agent_file), filtered by suffix — via find,
# not a bare glob, so this doesn't depend on shell-specific glob-qualifier syntax —
# so any hand-authored custom agents alongside them are preserved.
CLAUDE_AGENTS_DIR="$WORKING_DIR/.claude/agents"
if [[ -d "$CLAUDE_AGENTS_DIR" ]]; then
  if [[ -n "$(find "$CLAUDE_AGENTS_DIR" -maxdepth 1 -name '*-worker.md' -type f)" ]]; then
    find "$CLAUDE_AGENTS_DIR" -maxdepth 1 -name '*-worker.md' -type f -delete
    echo "  Removed .claude/agents/*-worker.md"
  fi
fi

# Step 5: Remove state directory
echo ""
echo "Step 5: Removing state directories..."
if [[ -d "$WORKING_DIR/.kiln" ]]; then
  rm -rf "$WORKING_DIR/.kiln"
  echo "  Removed .kiln/"
fi

# Step 6: Remove git hooks
echo ""
echo "Step 6: Removing git hooks..."
HOOK_PATH="$WORKING_DIR/.git/hooks/pre-push"
if [[ -f "$HOOK_PATH" ]]; then
  rm -f "$HOOK_PATH"
  echo "  Removed .git/hooks/pre-push"
fi

echo ""
echo "Cleanup complete."
echo "Kiln artifacts have been removed. The project is ready for a fresh kiln.sh run."

