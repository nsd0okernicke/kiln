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
KILN_PROJECT_DIR="$KILN_DIR/project"
WORKTREES_DIR="$WORKING_DIR/.worktrees"
ROLES_DIR="$KILN_DIR"
CONSTITUTION_FILE="$KILN_PROJECT_DIR/constitution.md"
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
.kiln
.worktrees/
.mcp.json
.claude/agents/*-worker.md
.codex/agents/*-worker.toml
EOF
    return
  fi

  # No trailing slash on .kiln: each worktree gets .kiln created as a symlink
  # back to the shared state dir (see the worktree_Kiln_dir assignment below),
  # and a trailing-slash pattern only matches real directories, not symlinks —
  # with the slash, the symlink stays untracked-but-not-ignored and can get
  # swept into a commit, later breaking merges that try to check it out.
  # Mirrors the same fix in kiln.ps1's Ensure-InitialGitignore.
  if ! grep -qxF '.kiln' "$gitignore_file"; then
    echo '.kiln' >> "$gitignore_file"
  fi

  if ! grep -qxF '.worktrees/' "$gitignore_file"; then
    echo '.worktrees/' >> "$gitignore_file"
  fi

  # .mcp.json and worker agent files (write_worker_agent_file) are regenerated
  # per-worktree/per-role with different content each time. If tracked, every
  # role's copy differs, so every /kiln-receive merge would hit an add/add
  # conflict — same rationale as kiln.ps1's Ensure-InitialGitignore. Scoped to
  # the *-worker.md suffix, not the whole .claude/agents/ dir, so a user's own
  # hand-authored custom agents there stay tracked.
  if ! grep -qxF '.mcp.json' "$gitignore_file"; then
    echo '.mcp.json' >> "$gitignore_file"
  fi

  if ! grep -qxF '.claude/agents/*-worker.md' "$gitignore_file"; then
    echo '.claude/agents/*-worker.md' >> "$gitignore_file"
  fi

  if ! grep -qxF '.codex/agents/*-worker.toml' "$gitignore_file"; then
    echo '.codex/agents/*-worker.toml' >> "$gitignore_file"
  fi
}

ensure_runtime_git_excludes() {
  local exclude_file
  exclude_file="$(git -C "$WORKING_DIR" rev-parse --git-path info/exclude)"
  mkdir -p "${exclude_file:h}"
  touch "$exclude_file"

  local pattern
  for pattern in ".kiln" ".worktrees/" ".mcp.json" ".claude/agents/*-worker.md" ".codex/agents/*-worker.toml"; do
    if ! grep -qxF "$pattern" "$exclude_file"; then
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

  # Seed framework tools into project state directory so agents can invoke set-status.py.
  # tools/ lives under kiln/framework/ (framework-owned) but, unlike the rest of that bucket,
  # gets copied fresh into the project's ephemeral .kiln/tools/ on every launch rather than
  # referenced by path or copied once at scaffold time.
  local framework_tools_dir="$(dirname "$SCRIPT_DIR")/kiln/framework/tools"
  if [[ -d "$framework_tools_dir" ]]; then
    mkdir -p "$STATE_DIR/tools"
    cp -r "$framework_tools_dir/." "$STATE_DIR/tools/"
  fi
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
    local channel_script="$(dirname "$SCRIPT_DIR")/kiln/framework/mcp-server/channel.py"
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

    # Copy worker agent definitions into worktree's .claude/agents/ so Claude Code can find them
    local worktree_agents_dir="$worktree_path/.claude/agents"
    mkdir -p "$worktree_agents_dir"
    local project_agents_dir="$WORKING_DIR/.claude/agents"
    if [[ -d "$project_agents_dir" ]]; then
      find "$project_agents_dir" -maxdepth 1 -name "*-worker.md" -exec cp {} "$worktree_agents_dir/" \;
    fi

    # Copy worker agent definitions into worktree's .codex/agents/ so Codex CLI's
    # project-scoped custom-agent discovery can find them
    local worktree_codex_agents_dir="$worktree_path/.codex/agents"
    mkdir -p "$worktree_codex_agents_dir"
    local project_codex_agents_dir="$WORKING_DIR/.codex/agents"
    if [[ -d "$project_codex_agents_dir" ]]; then
      find "$project_codex_agents_dir" -maxdepth 1 -name "*-worker.toml" -exec cp {} "$worktree_codex_agents_dir/" \;
    fi

    echo "$branch_name" >> "$WORKING_DIR/.git/kiln-sub-branches"
  done
}

# Mirrors Prepare-Skills in kiln.ps1: symlinks (the Unix equivalent of Windows' NTFS
# junction) every skill directory from the project's own kiln/project/skills/ into each
# claude/copilot role's .claude/skills/ or .github/skills/, so agents can actually invoke
# them. Recreated every run so removed skills don't linger. Unlike kiln.ps1, no separate
# synthetic "root" entry is needed here — WORKTREE_PATHS[$i] already resolves to
# $WORKING_DIR for @current/none/master roles (see the worktree_path_for_name call site),
# so looping over all roles already covers them.
prepare_skills() {
  local skills_source="$KILN_PROJECT_DIR/skills"
  if [[ ! -d "$skills_source" ]]; then
    echo -e "  ${YELLOW}[skills] No skills directory found at: $skills_source${RESET}"
    return
  fi

  local skill_count
  skill_count=$(find "$skills_source" -maxdepth 1 -mindepth 1 -type d | wc -l)
  if [[ "$skill_count" -eq 0 ]]; then
    echo -e "  ${YELLOW}[skills] Skills directory exists but is empty${RESET}"
    return
  fi
  echo -e "  ${CYAN}[skills] Found $skill_count skill(s) in $skills_source${RESET}"

  local i role agent worktree_path skills_dir skill_dir skill_name relative_skill_path
  for (( i = 1; i <= ${#ROLES[@]}; i++ )); do
    role="${ROLES[$i]}"
    agent="${AGENTS[$i]}"
    [[ "$agent" == "claude" || "$agent" == "copilot" ]] || continue
    worktree_path="${WORKTREE_PATHS[$i]}"

    if [[ "$agent" == "copilot" ]]; then
      skills_dir="$worktree_path/.github/skills"
    else
      skills_dir="$worktree_path/.claude/skills"
    fi

    # Always recreate so removed skills don't linger
    rm -rf "$skills_dir" 2>/dev/null || true
    mkdir -p "$skills_dir"

    for skill_dir in "$skills_source"/*/; do
      [[ -d "$skill_dir" ]] || continue
      skill_name="$(basename "$skill_dir")"
      relative_skill_path="$(python -c "import os; print(os.path.relpath('$skill_dir', '$skills_dir'))")" 2>/dev/null || true
      if [[ -n "$relative_skill_path" ]]; then
        ln -s "$relative_skill_path" "$skills_dir/$skill_name" 2>/dev/null \
          && echo -e "    ${GREEN}[$role] → $skill_name${RESET}" \
          || echo -e "    ${RED}[$role] ✗ Failed to link $skill_name${RESET}" >&2
      fi
    done
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

# Mirrors Prepare-CodexConfigs in kiln.ps1. Unlike Copilot (one shared global
# ~/.copilot/mcp-config.json), each Codex role gets its own isolated CODEX_HOME under
# .kiln/codex-home/<role>/ with its own config.toml. CODEX_HOME is a real, confirmed-working
# Codex CLI env var for relocating its entire config dir — using it instead of overwriting
# the user's real ~/.codex/config.toml avoids clobbering their own model/sandbox/profile
# settings the way Copilot's single-global-file approach would. kiln-db only (no
# kiln-channel): Codex has no confirmed support for a long-blocking MCP tool call, so codex
# roles poll instead — same limitation as Copilot today (see TODO.md Track D).
prepare_codex_configs() {
  local i role codex_home db_path
  db_path="$STATE_DIR/messages.db"

  for (( i = 1; i <= ${#AGENTS[@]}; i++ )); do
    [[ "${AGENTS[$i]}" == "codex" ]] || continue
    role="${ROLES[$i]}"
    codex_home="$STATE_DIR/codex-home/$role"
    mkdir -p "$codex_home"
    cat > "$codex_home/config.toml" << EOF
[mcp_servers.kiln-db]
command = "npx"
args = ["mcp-sqlite", "$db_path"]
EOF
    echo -e "${GREEN}Created CODEX_HOME config for role '$role' at $codex_home${RESET}"
  done
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

Read kiln/project/constitution.md for workflow and routing rules.
Read .claude/agents/${role}-worker.md to see what the worker subagent does (do not replicate it yourself).

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
    cat "$KILN_PROJECT_DIR/roles/${role}.md"
    if [[ -f "$KILN_PROJECT_DIR/constitution/project.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_PROJECT_DIR/constitution/project.md"
    fi
    if [[ -f "$KILN_PROJECT_DIR/constitution/engineering.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_PROJECT_DIR/constitution/engineering.md"
    fi
  } > "$out_path"
}

# Codex's own multi-agent spawn tools (spawn_agent/assign_agent_task/wait_agent/close_agent
# — the "multi_agent" feature, stable and enabled by default, confirmed against official
# docs at developers.openai.com/codex/subagents and directly against a live codex.exe
# install) give it real worker delegation, the same shape as Claude's Agent tool or
# Copilot's custom agents. Mirrors write_agent_instruction_file (Claude's thin wrapper),
# but points at Codex's project-scoped custom-agent convention (.codex/agents/*.toml)
# instead of .claude/agents/*.md, and names the spawn tools explicitly since Codex has no
# single named tool like Claude's "Agent tool".
write_codex_instructions_file() {
  local role="$1"
  local prompt_file="$2"

  cat > "$prompt_file" <<EOF
# Wrapper Agent — Message Loop Only

**Your role: LISTEN → DELEGATE → SEND. Nothing else.**

Do not do any of the ${role^^} work yourself. You are a thin wrapper that:
1. Polls for messages via \`read_query\` (no blocking channel — see loop below)
2. Delegates work to the \`${role}-worker\` custom agent using your multi-agent spawn
   tools (\`spawn_agent\`/\`assign_agent_task\`/\`wait_agent\`/\`close_agent\`)
3. Sends completed work via \`write_query\`
4. Repeats

The worker has all the ${role} role rules, quality gates, and standards baked into its
\`developer_instructions\` (\`.codex/agents/${role}-worker.toml\`) and no \`mcp_servers\`
configured — it cannot send or receive messages, only this wrapper session does that.

Read kiln/project/constitution.md for workflow and routing rules.
Read kiln/project/constitution/workflow.md for the handoff routing table and Commit
Convention (commit message format) — this prompt does not repeat them.

## Kiln Runtime Paths

- **Project root**: $WORKING_DIR
- **Message database**: $WORKING_DIR/.kiln/messages.db (access via MCP \`kiln-db\` server —
  \`read_query\`/\`write_query\` tools; no blocking channel, use the polling loop below)
- **Branch**: $current_branch — this is the ROOT branch. Do NOT substitute your worktree
  sub-branch (e.g. \`${current_branch}-${role}\` would be wrong).
- **Temporary files**: \`./tmp/\` (in your assigned worktree)

## Interaction Loop

Repeat this sequence indefinitely. **Do not stop after completing work — the loop is not
complete until the handoff is sent (step 8).**

1. **Poll** — call \`read_query\`:
   \`\`\`sql
   SELECT id, sender, content, created_at FROM messages
   WHERE target='${role}' AND branch='$current_branch' AND status='queued'
   ORDER BY priority ASC, created_at ASC LIMIT 1
   \`\`\`
   If empty, wait 15 seconds and repeat step 1. When found, mark it delivered:
   \`\`\`sql
   UPDATE messages SET status='delivered', delivered_at=datetime('now') WHERE id='<id>'
   \`\`\`
2. **Merge** — extract \`Branch:\`/\`Commit:\` from the message content, then run:
   \`git merge <commit-hash>\`. This merge commit becomes the squash anchor for step 7.
3. **Log received** — append a logbook.md entry: timestamp, full message content.
4. **Delegate the work** — do not implement anything yourself. Delegate this task entirely
   to the custom agent named \`${role}-worker\` using your multi-agent spawn tools. Give it
   the full content of the received message, your current branch/worktree, and an explicit
   request for a final report of what was implemented/verified and which files were
   touched. For system-communication-test messages: skip delegation entirely — forward the
   message as-is to the routing target from workflow.md and skip steps 5-8.
5. **Handle a failed or blocked report** — if the worker's report says it could not finish,
   delegate to it again once more, in this same turn, including its failure report as
   feedback. If it fails a second time, proceed to step 6 with a handoff that reports the
   blocker instead of normal work.
6. **Log sent** — append a logbook.md entry: timestamp, brief summary. Commit as part of
   the squash in step 7.
7. **Squash** — squash all your commits since the merge commit:
   \`\`\`sh
   LAST_MERGE=\$(git log --merges -1 --format="%H")
   git reset --soft "\${LAST_MERGE:-\$(git rev-list --max-parents=0 HEAD)}"
   git commit -m "<format from workflow.md Commit Convention>"
   \`\`\`
8. **Send handoff** — call \`write_query\` to INSERT into \`messages\` with the target and
   branch from workflow.md's routing table, and \`content\` formatted per Handoff Message
   Format in Workflow Rules. Verify:
   \`SELECT id FROM messages WHERE sender='${role}' AND branch='$current_branch' ORDER BY created_at DESC LIMIT 1\`
   If no row is found, INSERT again before returning to step 1.
9. Return to step 1.
EOF
}

# Mirrors write_worker_agent_file (Claude), but writes Codex CLI's project-scoped
# custom-agent TOML format (.codex/agents/<role>-worker.toml) instead of a Markdown file
# with YAML frontmatter. Required TOML fields per official docs: name, description,
# developer_instructions. mcp_servers = {} excludes messaging access, mirroring the
# Claude/Copilot worker's isolation. developer_instructions uses a TOML literal string
# ('''...''') rather than a basic string so the role/constitution content's own backticks,
# quotes, and any backslashes don't need escaping.
write_codex_worker_agent_file() {
  local role="$1"

  local agents_dir="$WORKING_DIR/.codex/agents"
  mkdir -p "$agents_dir"
  local out_path="$agents_dir/${role}-worker.toml"
  local timestamp
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"

  {
    echo "# Auto-generated by kiln.sh on $timestamp"
    echo "# DO NOT EDIT MANUALLY"
    echo
    echo "name = \"${role}-worker\""
    echo "description = \"Performs the ${role} role's implementation work for one handoff cycle. Dispatched by the persistent ${role} shell agent's message loop; not for direct/standalone use.\""
    echo "mcp_servers = {}"
    echo "developer_instructions = '''"
    cat "$KILN_PROJECT_DIR/roles/${role}.md"
    if [[ -f "$KILN_PROJECT_DIR/constitution/project.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_PROJECT_DIR/constitution/project.md"
    fi
    if [[ -f "$KILN_PROJECT_DIR/constitution/engineering.md" ]]; then
      echo
      echo "---"
      echo
      cat "$KILN_PROJECT_DIR/constitution/engineering.md"
    fi
    echo "'''"
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

  if [[ "$agent" == "codex" ]]; then
    write_codex_instructions_file "$role" "$prompt_file"
  else
    write_agent_instruction_file "$role" "$prompt_file" "$role_worktree"
  fi
  if [[ "$agent" == "claude" ]]; then
    write_worker_agent_file "$role"
  elif [[ "$agent" == "codex" ]]; then
    write_codex_worker_agent_file "$role"
  fi

  case "$agent" in
    claude)
      launch_cmd="export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && claude --mcp-config ./.mcp.json --append-system-prompt-file '$prompt_file' --permission-mode acceptEdits -n 'Kiln ${display}' \"\$(cat '$prompt_file')\""
      ;;
    codex)
      launch_cmd="export CODEX_HOME='$STATE_DIR/codex-home/$role' && export PATH='$SCRIPT_DIR':\$PATH && cd '$role_worktree' && codex -C '$role_worktree' --dangerously-bypass-approvals-and-sandbox \"\$(cat '$prompt_file')\""
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
  prepare_codex_configs

  # Create tmp directory for temporary files (used by all agents)
  mkdir -p "$WORKING_DIR/tmp"

  prepare_skills

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

