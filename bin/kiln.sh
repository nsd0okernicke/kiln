#!/usr/bin/env zsh
set -euo pipefail

SESSION_PREFIX="Kiln"
AGENT_WINDOW="swarm"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

DRY_RUN=0
WORKING_DIR=""
CONFIG_PROFILE=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --profile=*)
      CONFIG_PROFILE="${arg#--profile=}"
      ;;
    --profile)
      CONFIG_PROFILE="$2"
      shift
      ;;
    -*) echo "Unknown flag: $arg" >&2; exit 1 ;;
    *) [[ -z "$WORKING_DIR" ]] && WORKING_DIR="$arg" ;;
  esac
done
WORKING_DIR="${WORKING_DIR:-$PWD}"
WORKING_DIR="$(cd "$WORKING_DIR" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KILN_DIR="$WORKING_DIR/kiln"
WORKTREES_DIR="$WORKING_DIR/.worktrees"
ROLES_DIR="$KILN_DIR"
CONSTITUTION_FILE="$KILN_DIR/constitution.md"
STATE_DIR="$WORKING_DIR/.kiln"
WINDOW_IDS_FILE="$STATE_DIR/window-ids"
WINDOW_STATE_FILE="$STATE_DIR/windows.tsv"
WINDOW_WATCHDOG_LOG="$STATE_DIR/window-watchdog.log"
PROMPTS_DIR="$STATE_DIR/prompts"
TMUX_SOCKET_DIR="/private/tmp/Kiln-${UID}"
PROJECT_SOCKET_ID="$(printf '%s' "$WORKING_DIR" | cksum)"
PROJECT_SOCKET_ID="${PROJECT_SOCKET_ID%% *}"
TMUX_SOCKET="$TMUX_SOCKET_DIR/$PROJECT_SOCKET_ID.sock"
TMUX_SOCKET_FILE="$STATE_DIR/tmux-socket"
TERMINAL_BACKEND=""

typeset -a ROLES=()
typeset -a AGENTS=()
typeset -a SESSIONS=()
typeset -a DISPLAY_NAMES=()
typeset -a WORKTREE_NAMES=()
typeset -a WORKTREE_PATHS=()
typeset -A ROLE_INDEX=()
typeset -A WORKTREE_INDEX=()
typeset -i CLEANUP_OWNER_INDEX=1
typeset -i TMUX_WINDOW_BASE_INDEX=0
typeset -i TMUX_PANE_BASE_INDEX=0
typeset -i i=0

check_dependency() {
  if ! command -v "$1" &>/dev/null; then
    echo -e "${RED}Error:${RESET} '$1' is required but not installed."
    exit 1
  fi
}

get_tmux_option() {
  local option="$1"
  local scope="$2"
  local default_value="$3"
  local value=""

  case "$scope" in
    session)
      value="$(tmux -S "$TMUX_SOCKET" show-options -gqv "$option" 2>/dev/null || true)"
      ;;
    window)
      value="$(tmux -S "$TMUX_SOCKET" show-window-options -gqv "$option" 2>/dev/null || true)"
      ;;
  esac

  if [[ "$value" == <-> ]]; then
    echo "$value"
  else
    echo "$default_value"
  fi
}

detect_tmux_base_indexes() {
  local probe_session=""

  mkdir -p "$TMUX_SOCKET_DIR"
  if ! tmux -S "$TMUX_SOCKET" info >/dev/null 2>&1; then
    probe_session="Kiln-probe-$$"
    tmux -S "$TMUX_SOCKET" new-session -d -s "$probe_session" "sleep 60" >/dev/null
  fi

  TMUX_WINDOW_BASE_INDEX="$(get_tmux_option base-index session 0)"
  TMUX_PANE_BASE_INDEX="$(get_tmux_option pane-base-index window 0)"

  if [[ -n "$probe_session" ]]; then
    tmux -S "$TMUX_SOCKET" kill-session -t "$probe_session" >/dev/null 2>&1 || true
  fi
}

tmux_agent_target() {
  local session="$1"
  local window="$2"

  echo "${session}:${window}.${TMUX_PANE_BASE_INDEX}"
}

ensure_initial_gitignore() {
  local gitignore_file="$WORKING_DIR/.gitignore"

  if [[ ! -f "$gitignore_file" ]]; then
    cat > "$gitignore_file" <<'EOF'
.kiln/
.worktrees/
EOF
    return
  fi

  if ! grep -qx '.kiln/' "$gitignore_file"; then
    echo '.kiln/' >> "$gitignore_file"
  fi

  if ! grep -qx '.worktrees/' "$gitignore_file"; then
    echo '.worktrees/' >> "$gitignore_file"
  fi

}

ensure_runtime_git_excludes() {
  local exclude_file
  exclude_file="$(git -C "$WORKING_DIR" rev-parse --git-path info/exclude)"
  mkdir -p "${exclude_file:h}"
  touch "$exclude_file"

  local pattern
  for pattern in ".kiln/" ".worktrees/"; do
    if ! grep -qx "$pattern" "$exclude_file"; then
      echo "$pattern" >> "$exclude_file"
    fi
  done
}

initialize_git_repo() {
  if [[ -d "$WORKING_DIR/.git" ]]; then
    return
  fi

  git init "$WORKING_DIR" >/dev/null
  git -C "$WORKING_DIR" branch -M master >/dev/null
  ensure_initial_gitignore
  git -C "$WORKING_DIR" add .
  git -C "$WORKING_DIR" commit -m "Initial Kiln repository" >/dev/null
}

install_git_hooks() {
  local hook_path="$WORKING_DIR/.git/hooks/pre-push"
  [[ -f "$hook_path" ]] && return

  mkdir -p "${hook_path:h}"

  cat > "$hook_path" << 'EOF'
#!/bin/sh
BRANCH_LIST="$(git rev-parse --git-dir)/kiln-sub-branches"
while read local_ref local_sha remote_ref remote_sha; do
  branch="${local_ref#refs/heads/}"
  if [ -f "$BRANCH_LIST" ] && grep -qxF "$branch" "$BRANCH_LIST"; then
    echo "error: '$branch' is a Kiln sub-branch and cannot be pushed."
    exit 1
  fi
done
exit 0
EOF
  chmod +x "$hook_path"
}

has_command() {
  command -v "$1" &>/dev/null
}

source "$SCRIPT_DIR/../lib/terminal-adapter.sh"

remove_nonessential_clone_files() {
  if [[ "${WORKING_DIR:t}" == "swarm-forge" ]]; then
    return
  fi

  if [[ -d "$STATE_DIR" ]]; then
    return
  fi

  rm -rf "$WORKING_DIR/README.md" "$WORKING_DIR/KilnInitSpec.md" "$WORKING_DIR/examples"
}

display_name_for_role() {
  local role="$1"
  local normalized="${role//[-_]/ }"
  local -a parts
  local part
  local label=""

  parts=(${=normalized})
  for part in "${parts[@]}"; do
    part="${(C)part}"
    if [[ -n "$label" ]]; then
      label+=" "
    fi
    label+="$part"
  done

  echo "$label"
}

session_name_for_role() {
  echo "${SESSION_PREFIX}-$1"
}

worktree_path_for_name() {
  echo "$WORKTREES_DIR/$1"
}

load_config_from_profile() {
  if [[ ! -f "$CONSTITUTION_FILE" ]]; then
    echo -e "${RED}Error:${RESET} Constitution file not found at $CONSTITUTION_FILE"
    exit 1
  fi

  local i role worktree agent
  i=0
  while (( i < TERMINAL_COUNT )); do
    eval "role=\$TERMINAL_${i}_ROLE"
    eval "worktree=\$TERMINAL_${i}_WORKTREE"
    eval "agent=\$TERMINAL_${i}_AGENT"

    if [[ -n "$role" ]]; then
      # Default to claude if agent not specified in profile
      if [[ -z "$agent" ]]; then
        agent="claude"
      fi

      if [[ -n "${ROLE_INDEX[$role]:-}" ]]; then
        echo -e "${RED}Error:${RESET} Duplicate role '$role'"
        exit 1
      fi

      if [[ "$worktree" != "none" && "$worktree" != "master" && "$worktree" != "@current" && -n "${WORKTREE_INDEX[$worktree]:-}" ]]; then
        echo -e "${RED}Error:${RESET} Duplicate worktree '$worktree'"
        exit 1
      fi

      if [[ "$worktree" == *"/"* || "$worktree" == "." || "$worktree" == ".." ]]; then
        echo -e "${RED}Error:${RESET} Invalid worktree '$worktree' for role '$role'"
        exit 1
      fi

      case "$agent" in
        claude|codex|copilot|grok) ;;
        *)
          echo -e "${RED}Error:${RESET} Unsupported agent '$agent' for role '$role'"
          exit 1
          ;;
      esac

      ROLE_INDEX[$role]=${#ROLES[@]}
      if [[ "$worktree" != "none" && "$worktree" != "master" && "$worktree" != "@current" ]]; then
        WORKTREE_INDEX[$worktree]=${#ROLES[@]}
      fi
      ROLES+=("$role")
      AGENTS+=("$agent")
      SESSIONS+=("$(session_name_for_role "$role")")
      DISPLAY_NAMES+=("$(display_name_for_role "$role")")
      WORKTREE_NAMES+=("$worktree")
      if [[ "$worktree" == "none" || "$worktree" == "master" || "$worktree" == "@current" ]]; then
        WORKTREE_PATHS+=("$WORKING_DIR")
      else
        WORKTREE_PATHS+=("$(worktree_path_for_name "$worktree")")
      fi
    fi
    i=$((i + 1))
  done

  if (( ${#ROLES[@]} == 0 )); then
    echo -e "${RED}Error:${RESET} No windows defined in profile"
    exit 1
  fi
}

check_helper_scripts() {
  local helper
  for helper in kiln-window-watchdog.sh terminal-adapter.sh; do
    if [[ ! -x "$SCRIPT_DIR/../lib/$helper" ]]; then
      echo -e "${RED}Error:${RESET} Required helper script not found or not executable: $SCRIPT_DIR/../lib/$helper"
      exit 1
    fi
  done

  for helper in terminal-app.sh ghostty.sh windows-terminal.sh none.sh; do
    if [[ ! -x "$SCRIPT_DIR/../lib/terminal-adapters/$helper" ]]; then
      echo -e "${RED}Error:${RESET} Required terminal adapter not found or not executable: $SCRIPT_DIR/../lib/terminal-adapters/$helper"
      exit 1
    fi
  done
}

prepare_workspace() {
  mkdir -p "$STATE_DIR" "$PROMPTS_DIR" "$WORKTREES_DIR" "$TMUX_SOCKET_DIR"
  printf '%s\n' "$TMUX_SOCKET" > "$TMUX_SOCKET_FILE"
  check_helper_scripts
}


prepare_worktrees() {
  local i worktree_name worktree_path branch_name agent
  : > "$WORKING_DIR/.git/kiln-sub-branches"

  for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
    worktree_name="${WORKTREE_NAMES[$i]}"
    worktree_path="${WORKTREE_PATHS[$i]}"
    branch_name="${current_branch}-${worktree_name}"
    agent="${AGENTS[$i]}"

    if [[ "$worktree_name" == "none" || "$worktree_name" == "master" || "$worktree_name" == "@current" ]]; then
      continue
    fi

    if [[ ! -e "$worktree_path/.git" && ! -d "$worktree_path/.git" ]]; then
      git -C "$WORKING_DIR" worktree add --force -B "$branch_name" "$worktree_path" HEAD >/dev/null
    fi

    # Create symlink to shared .kiln directory for direct database access
    local worktree_Kiln_dir="$worktree_path/.kiln"
    if [[ -e "$worktree_Kiln_dir" ]]; then
      rm -rf "$worktree_Kiln_dir" 2>/dev/null || true
    fi

    # Create relative symlink for portability
    local relative_state_path
    relative_state_path="$(python -c "import os; print(os.path.relpath('$STATE_DIR', '$worktree_path'))")" 2>/dev/null || true

    if [[ -n "$relative_state_path" ]]; then
      ln -s "$relative_state_path" "$worktree_Kiln_dir" 2>/dev/null || {
        echo -e "  ${YELLOW}Warning: Could not create symlink, falling back to copy${RESET}" >&2
        mkdir -p "$worktree_Kiln_dir"
        if [[ -d "$STATE_DIR/tools" ]]; then
          cp -r "$STATE_DIR/tools" "$worktree_Kiln_dir/" 2>/dev/null || true
        fi
        mkdir -p "$worktree_Kiln_dir/logs"
      }
    fi

    # Create .mcp.json in worktree root with MCP server configuration
    local db_path="$STATE_DIR/messages.db"
    local channel_script="$(dirname "$SCRIPT_DIR")/kiln/mcp-server/channel.py"
    local logs_dir="$STATE_DIR/logs"
    mkdir -p "$logs_dir"
    local channel_log="$logs_dir/channel-$role.log"
    cat > "$worktree_path/.mcp.json" << EOF
{
  "name": "kiln-$role",
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$db_path"]
    },
    "kiln-channel": {
      "command": "python",
      "args": ["$channel_script"],
      "env": {
        "KILN_ROLE": "$role",
        "KILN_DB_PATH": "$db_path",
        "KILN_BRANCH": "$branch_name",
        "KILN_CHANNEL_LOG": "$channel_log"
      }
    }
  }
}
EOF

    # Create tmp and status directories for temporary files and agent state
    mkdir -p "$worktree_path/tmp"
    mkdir -p "$STATE_DIR/status"

    echo "$branch_name" >> "$WORKING_DIR/.git/kiln-sub-branches"
  done
}

prepare_agent_configs() {
  # Create ~/.copilot/mcp-config.json for Copilot agents (if any exist in the swarm)
  local i db_path has_copilot
  db_path="$STATE_DIR/messages.db"
  has_copilot=0

  for (( i = 1; i <= ${#AGENTS[@]}; i++ )); do
    if [[ "${AGENTS[$i]}" == "copilot" ]]; then
      has_copilot=1
      break
    fi
  done

  if [[ $has_copilot -eq 1 ]]; then
    # Create .mcp.json in project root (Copilot agents look here for MCP config)
    cat > "$WORKING_DIR/.mcp.json" << EOF
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$db_path"]
    }
  }
}
EOF
    echo -e "${GREEN}Created .mcp.json (MCP server configuration)${RESET}"
  fi
}

check_backend_dependencies() {
  local i
  for (( i = 1; i <= ${#AGENTS[@]}; i++ )); do
    case "${AGENTS[$i]}" in
      claude) check_dependency claude ;;
      codex) check_dependency codex ;;
      copilot) check_dependency copilot ;;
      grok) check_dependency grok ;;
    esac
  done
}

create_role_session() {
  local session="$1"
  local title="$2"

  tmux -S "$TMUX_SOCKET" new-session -d -s "$session" -n "$AGENT_WINDOW"
  tmux -S "$TMUX_SOCKET" rename-window -t "$session:$AGENT_WINDOW" "$title"
  tmux -S "$TMUX_SOCKET" set-window-option -t "$session:$title" allow-rename off
}

write_agent_instruction_file() {
  local role="$1"
  local prompt_file="$2"
  local worktree_path="$3"

  cat > "$prompt_file" <<EOF
# Wrapper Agent — Message Loop Only

**Your role: LISTEN → DELEGATE → SEND. Nothing else.**

Do not do any of the ${role^^} work yourself. You are a thin wrapper that:
1. Listens for messages via \`/kiln-receive\`
2. Wraps/delegates work to the \`${role}-worker\` subagent via Agent tool
3. Sends completed work via \`/kiln-handoff\`
4. Repeats

The worker subagent has all the ${role} role rules, quality gates, and standards. Your job is the message loop only.

Read Kiln/constitution.md for workflow and routing rules.
Read Kiln/.claude/agents/${role}-worker.md to see what the worker subagent does (do not replicate it yourself).

## Kiln Runtime Paths

- **Project root**: \`$WORKING_DIR\`
- **Message database**: \`$WORKING_DIR/.kiln/messages.db\` (access via MCP \`Kiln-db\` server)
- **Temporary files**: \`./tmp/\` (in your assigned worktree)
EOF
}

# Generates the per-role worker subagent definition consumed by Claude Code's Agent/Task
# tool (.claude/agents/<role>-worker.md). Mirrors Write-GeneratedWorkerAgent in kiln.ps1:
# role.md + project/engineering constitution, no workflow.md (handoff/messaging stays the
# shell's concern) and no Agent/MCP tools (no recursive spawning, no messaging).
#
# Generated in the main project's .claude/agents/ directory (not the worktree's),
# so Claude Code's agent discovery finds it when the shell agent (running in the
# worktree) spawns the subagent via the Agent tool.
write_worker_agent_file() {
  local role="$1"

  local agents_dir="$WORKING_DIR/.claude/agents"
  mkdir -p "$agents_dir"
  local out_path="$agents_dir/${role}-worker.md"
  local timestamp
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"

  {
    echo "---"
    echo "name: ${role}-worker"
    echo "description: Performs the ${role} role's implementation work for one handoff cycle. Dispatched by the persistent ${role} shell agent's message loop; not for direct/standalone use."
    echo "tools: Read, Write, Edit, Glob, Grep, Bash, Skill, NotebookEdit, TodoWrite"
    echo "---"
    echo
    echo "<!-- Auto-generated by kiln.sh on $timestamp -->"
    echo "<!-- DO NOT EDIT MANUALLY -->"
    echo
    cat "$KILN_DIR/roles/${role}.md"
    if [[ -f "$KILN_DIR/constitution/project.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_DIR/constitution/project.md"
    fi
    if [[ -f "$KILN_DIR/constitution/engineering.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_DIR/constitution/engineering.md"
    fi
  } > "$out_path"
}

send_initial_grok_prompt() {
  local session="$1"
  local display="$2"
  local prompt_file="$3"

  (
    sleep 3
    tmux -S "$TMUX_SOCKET" send-keys -t "$(tmux_agent_target "$session" "$display")" -l -- "$(< "$prompt_file")"
    sleep 0.15
    tmux -S "$TMUX_SOCKET" send-keys -t "$(tmux_agent_target "$session" "$display")" C-m
    sleep 0.05
    tmux -S "$TMUX_SOCKET" send-keys -t "$(tmux_agent_target "$session" "$display")" C-j
  ) &!
}

launch_role() {
  local index="$1"
  local role="${ROLES[$index]}"
  local agent="${AGENTS[$index]}"
  local session="${SESSIONS[$index]}"
  local display="${DISPLAY_NAMES[$index]}"
  local role_worktree="${WORKTREE_PATHS[$index]}"
  local prompt_file="$PROMPTS_DIR/${role}.md"
  local launch_cmd=""

  write_agent_instruction_file "$role" "$prompt_file" "$role_worktree"
  if [[ "$agent" == "claude" ]]; then
    write_worker_agent_file "$role"
  fi

  case "$agent" in
    claude)
      launch_cmd="export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && claude --mcp-config ./.mcp.json --append-system-prompt-file '$prompt_file' --permission-mode acceptEdits -n 'Kiln ${display}' \"\$(cat '$prompt_file')\""
      ;;
    codex)
      launch_cmd="export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && codex -C '$role_worktree' \"\$(cat '$prompt_file')\""
      ;;
    copilot)
      launch_cmd="export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && copilot --allow-all --name 'Kiln ${display}' -i \"\$(cat '$prompt_file')\""
      ;;
    grok)
      launch_cmd="export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && grok --cwd '$role_worktree' --permission-mode acceptEdits --rules \"\$(cat '$prompt_file')\""
      ;;
  esac

  tmux -S "$TMUX_SOCKET" send-keys -t "$(tmux_agent_target "$session" "$display")" "$launch_cmd" Enter
  if [[ "$agent" == "grok" ]]; then
    send_initial_grok_prompt "$session" "$display" "$prompt_file"
  fi
  echo -e "  ${CYAN}[${display}]${RESET} started in session ${session}"
}

choose_cleanup_owner() {
  CLEANUP_OWNER_INDEX=1
}

check_dependency tmux
check_dependency git
detect_tmux_base_indexes
remove_nonessential_clone_files

if (( DRY_RUN )); then
  echo -e "${YELLOW}[DRY RUN] No files or processes will be created.${RESET}"
fi

if (( ! DRY_RUN )); then
  initialize_git_repo
  install_git_hooks
fi
current_branch=$(git -C "$WORKING_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
[[ -z "$current_branch" || "$current_branch" == "HEAD" ]] && current_branch="Kiln"

if (( ! DRY_RUN )); then
  ensure_runtime_git_excludes
fi

# Load configuration profile (default to 'dev' if not specified)
if [[ -z "$CONFIG_PROFILE" ]]; then
  CONFIG_PROFILE="dev"
fi

echo -e "${CYAN}Loading configuration profile: $CONFIG_PROFILE${RESET}"
source "$SCRIPT_DIR/../lib/profile-loader.sh"

if eval "$(load_Kiln_profile "$WORKING_DIR" "$CONFIG_PROFILE")"; then
  :
else
  echo -e "${RED}Error: Failed to load profile '$CONFIG_PROFILE'${RESET}" >&2
  exit 1
fi

load_config_from_profile
check_backend_dependencies
TERMINAL_BACKEND="$(detect_terminal_backend)"
load_terminal_backend "$TERMINAL_BACKEND"
export KILN_LAYOUT_JSON="${PROFILE_LAYOUT_JSON:-}"

if (( DRY_RUN )); then
  echo -e "${YELLOW}[DRY RUN] Would create workspace dirs: $STATE_DIR, $PROMPTS_DIR${RESET}"
  echo ""
  echo -e "${YELLOW}[DRY RUN] Would create worktrees:${RESET}"
  for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
    local wt_name="${WORKTREE_NAMES[$i]}"
    local role="${ROLES[$i]}"
    if [[ "$wt_name" == "none" || "$wt_name" == "master" || "$wt_name" == "@current" ]]; then
      echo -e "  ${CYAN}[$role]${RESET} runs in project root (no worktree)"
    else
      echo -e "  ${CYAN}[$role]${RESET} branch=${current_branch}-${wt_name} path=${WORKTREES_DIR}/${wt_name}"
    fi
  done
  echo ""
else
  prepare_workspace
  prepare_worktrees
  prepare_agent_configs

  # Create tmp directory for temporary files (used by all agents)
  mkdir -p "$WORKING_DIR/tmp"

  # Initialize message database (ACID-compliant message queuing via SQLite)
  echo -e "${CYAN}Initializing message database...${RESET}"
  messages_db="$STATE_DIR/messages.db"
  if [[ ! -f "$messages_db" ]]; then
    if command -v sqlite3 &>/dev/null; then
      sqlite3 "$messages_db" << 'SQL'
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  sender TEXT NOT NULL,
  target TEXT NOT NULL,
  priority INTEGER DEFAULT 50,
  status TEXT DEFAULT 'queued',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  acked_at TEXT,
  processed_at TEXT,
  error TEXT,
  branch TEXT NOT NULL DEFAULT 'main'
);
SQL
      if [[ $? -ne 0 ]]; then
        echo -e "${YELLOW}Warning: Could not initialize message database with sqlite3${RESET}"
      fi
    else
      echo -e "${YELLOW}Warning: sqlite3 not found; message database initialization skipped${RESET}"
    fi
  fi
fi
choose_cleanup_owner

if (( DRY_RUN )); then
  echo -e "${CYAN}${BOLD}"
  echo "  ╔═══════════════════════════════════════════════╗"
  echo "  ║           Kiln v1.0 Starting            ║"
  echo "  ║   Disciplined agents build better software    ║"
  echo "  ╚═══════════════════════════════════════════════╝"
  echo -e "${RESET}"
  echo -e "${YELLOW}[DRY RUN] Would launch agents via: $(terminal_backend_label)${RESET}"
  for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
    local agent="${AGENTS[$i]}"
    local display="${DISPLAY_NAMES[$i]}"
    local worktree="${WORKTREE_PATHS[$i]}"
    echo -e "  ${CYAN}[${display}]${RESET} ${agent} in ${worktree}"
  done
  exit 0
fi

local_session=""
for local_session in "${SESSIONS[@]}"; do
  [[ -n "$local_session" ]] || continue
  if tmux -S "$TMUX_SOCKET" has-session -t "$local_session" 2>/dev/null; then
    echo -e "${YELLOW}Existing Kiln session found: ${local_session}. Killing it...${RESET}"
    tmux -S "$TMUX_SOCKET" kill-session -t "$local_session"
  fi
done

echo -e "${CYAN}${BOLD}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║           Kiln v1.0 Starting            ║"
echo "  ║   Disciplined agents build better software    ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${RESET}"

echo -e "${GREEN}Launching Kiln tmux sessions...${RESET}"
for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
  create_role_session "${SESSIONS[$i]}" "${DISPLAY_NAMES[$i]}"
done

echo -e "${GREEN}Starting agents...${RESET}"
for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
  launch_role "$i"
done

echo ""
echo -e "${GREEN}${BOLD}Kiln is ready.${RESET}"
echo -e "Working directory: ${WORKING_DIR}"
echo -e "Sessions:"
for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
  echo -e "  ${DISPLAY_NAMES[$i]}: ${SESSIONS[$i]}"
done
echo ""
echo -e "${GREEN}Tip: Agents communicate via MCP SQLite at .kiln/messages.db${RESET}"
echo -e "${GREEN}Tip: Reattach manually with 'tmux -S $TMUX_SOCKET attach-session -t <session-name>' if needed.${RESET}"
echo ""

if terminal_backend_can_open_sessions; then
  echo -e "Opening separate $(terminal_backend_label) surfaces for each session..."
  if terminal_backend_tracks_windows; then
    : > "$WINDOW_IDS_FILE"
    : > "$WINDOW_STATE_FILE"
  fi
  previous_window_id=""
  for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
    window_id="$(terminal_open_session "${SESSIONS[$i]}" "Kiln ${DISPLAY_NAMES[$i]}" "$previous_window_id")"
    if terminal_backend_tracks_windows; then
      echo "$window_id" >> "$WINDOW_IDS_FILE"
      printf '%s\t%s\t%s\t%s\n' \
        "$i" \
        "$window_id" \
        "${SESSIONS[$i]}" \
        "Kiln ${DISPLAY_NAMES[$i]}" >> "$WINDOW_STATE_FILE"
      previous_window_id="$window_id"
    fi
  done
  if terminal_backend_tracks_windows; then
    nohup "$SCRIPT_DIR/../lib/kiln-window-watchdog.sh" \
      "$WINDOW_STATE_FILE" \
      "$WINDOW_IDS_FILE" \
      "$CLEANUP_OWNER_INDEX" \
      "$TMUX_SOCKET" \
      "$WORKING_DIR" \
      "$TERMINAL_BACKEND" > "$WINDOW_WATCHDOG_LOG" 2>&1 &
  else
    echo -e "${YELLOW}$(terminal_backend_label) surfaces are not trackable; window watchdog is disabled for this backend.${RESET}"
  fi
else
  echo -e "${YELLOW}No terminal backend found; attaching current shell to '${SESSIONS[$CLEANUP_OWNER_INDEX]}' instead.${RESET}"
  tmux -S "$TMUX_SOCKET" attach-session -t "${SESSIONS[$CLEANUP_OWNER_INDEX]}"
fi

