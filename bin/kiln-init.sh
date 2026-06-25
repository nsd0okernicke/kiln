#!/bin/bash

##
# Create a new Kiln project with full configuration and git initialization.
#
# Usage:
#   ./new-project.sh /path/to/project
#   ./new-project.sh /path/to/project --example library-hub
#   ./new-project.sh /path/to/project --profile dev
#   ./new-project.sh --list-profiles
##

set -e

PROFILE="dev"
EXAMPLE=""
TARGET=""
LIST_PROFILES=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile=*)
            PROFILE="${1#--profile=}"
            shift
            ;;
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --example=*)
            EXAMPLE="${1#--example=}"
            shift
            ;;
        --example)
            EXAMPLE="$2"
            shift 2
            ;;
        --list-profiles)
            LIST_PROFILES=1
            shift
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            [[ -z "$TARGET" ]] && TARGET="$1" || { echo "Unexpected argument: $1"; exit 1; }
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRAMEWORK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KILN_DIR="$FRAMEWORK_ROOT/kiln"

if [ ! -d "$KILN_DIR" ]; then
    echo "Error: Could not find Kiln framework directory."
    echo "This script must be run from the Kiln repository root (bin/new-project.sh)."
    exit 1
fi

# Handle --list-profiles flag
if [ $LIST_PROFILES -eq 1 ]; then
    echo "Available Kiln configuration profiles:"
    echo ""
    source "$FRAMEWORK_ROOT/lib/profile-loader.sh"
    get_available_profiles "$FRAMEWORK_ROOT/kiln"
    exit 0
fi

# Validate target
if [ -z "$TARGET" ]; then
    echo "Usage: $0 <target-path> [--example <example-name>] [--profile <profile-name>]"
    exit 1
fi

# Validate target doesn't exist
if [ -e "$TARGET" ]; then
    echo "Error: Target directory already exists: $TARGET"
    exit 1
fi

echo "Creating Kiln project: $TARGET"

# Create directory structure
mkdir -p "$TARGET/kiln/constitution"
mkdir -p "$TARGET/kiln/roles"
mkdir -p "$TARGET/kiln/skills"
mkdir -p "$TARGET/.kiln"
echo "✓ Created directory structure"

# Copy constitution files
for file in engineering.md workflow.md; do
    if [ -f "$KILN_DIR/constitution/$file" ]; then
        cp "$KILN_DIR/constitution/$file" "$TARGET/kiln/constitution/"
    fi
done
echo "✓ Copied constitution files"

# Copy role files
if [ -d "$KILN_DIR/roles" ]; then
    for file in "$KILN_DIR/roles"/*.md; do
        if [ -f "$file" ]; then
            cp "$file" "$TARGET/kiln/roles/"
        fi
    done
    echo "✓ Copied role files"
fi

# Copy skills directory
if [ -d "$KILN_DIR/skills" ]; then
    for skill_dir in "$KILN_DIR/skills"/*; do
        if [ -d "$skill_dir" ]; then
            cp -r "$skill_dir" "$TARGET/kiln/skills/"
        fi
    done
    echo "✓ Copied skills"
fi

# Copy MCP configuration
if [ -f "$FRAMEWORK_ROOT/kiln/.mcp.json" ]; then
    cp "$FRAMEWORK_ROOT/kiln/.mcp.json" "$TARGET/kiln/"
    echo "✓ Copied MCP configuration"
fi

# Note: Profiles are not copied to the target project.
# Projects inherit framework profiles; override by creating kiln.profiles.yaml at project root if needed.

# Create .mcp.json in project root with MCP server configuration (for Copilot agents)
db_path="$TARGET/.kiln/messages.db"
cat > "$TARGET/.mcp.json" << EOF
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$db_path"]
    }
  }
}
EOF
echo "✓ Created .mcp.json (MCP server configuration)"


# Write starter project.md
cat > "$TARGET/kiln/constitution/project.md" << 'EOF'
# Project Rules

- This project is configured for Kiln with four Codex-backed agents: specifier, coder, refactorer, and architect.
- Project language: Python.
- Preserve project-local Kiln configuration under `Kiln/`.
- Keep swarm state local under `.Kiln/` (SQLite message queue) and worktrees under `.worktrees/`.
- Prefer terse, explicit handoffs that report state and request role-appropriate review. Do not include verifications or sender process narrative.
- Do not change another role's prompt or workflow ownership without explicit user direction.
EOF
echo "✓ Created project.md"

# Write constitution.md
cat > "$TARGET/kiln/constitution.md" << 'EOF'
# Kiln Constitution

This file takes precedence over subordinate files.
Read and obey the following subordinate documents in order.

1. `Kiln/constitution/project.md`
2. `Kiln/constitution/engineering.md`
3. `Kiln/constitution/workflow.md`

If two subordinate files conflict, the earlier file wins.
EOF
echo "✓ Created constitution.md"


# Write Claude Code configuration (copy template from framework)
mkdir -p "$TARGET/.claude"
cat > "$TARGET/.claude/.gitignore" << 'EOF'
*
EOF

TEMPLATE_SETTINGS="$FRAMEWORK_ROOT/kiln/.claude/settings.json"
if [ -f "$TEMPLATE_SETTINGS" ]; then
    cp "$TEMPLATE_SETTINGS" "$TARGET/.claude/settings.json"
    echo "✓ Created .claude/settings.json"
else
    echo "Warning: Could not find Claude settings template at $TEMPLATE_SETTINGS" >&2
fi

# Copy example README if requested
if [ "$EXAMPLE" = "library-hub" ]; then
    if [ -f "$FRAMEWORK_ROOT/examples/library-hub/README.md" ]; then
        cp "$FRAMEWORK_ROOT/examples/library-hub/README.md" "$TARGET/README.md"
        echo "✓ Copied example README.md"
    fi
fi

# Initialize database
echo ""
echo "Initializing database..."
messages_db="$TARGET/.kiln/messages.db"
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
CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status);
SQL
    if [ $? -eq 0 ]; then
        echo "✓ Initialized message database"
    else
        echo "Warning: Failed to initialize database with sqlite3" >&2
    fi
else
    echo "Warning: sqlite3 not found; message database initialization skipped" >&2
fi

# Initialize git
echo ""
echo "Initializing git repository..."

if ! command -v git &> /dev/null; then
    echo "Error: Git is not installed or not in PATH."
    exit 1
fi

(
    cd "$TARGET"
    git init >/dev/null
    git branch -M main 2>/dev/null || true

    # Create .gitignore
    cat > .gitignore << 'GITIGNORE'
.DS_Store
.env
.env.local
*.pyc
__pycache__/
*.egg-info/
dist/
build/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/
.venv/
venv/
.idea/
.vscode/
*.swp
*.swo
*~
.Kiln/
.worktrees/
GITIGNORE
)

echo "✓ Initialized git repository on branch 'main'"
echo ""
echo "✓ Project created successfully: $TARGET"
echo ""
echo "Next steps:"
echo "  1. cd $TARGET"
echo "  2. Review Kiln/constitution/ and Kiln/roles/"
echo "  3. git add Kiln/ .claude/ && git commit -m 'Add Kiln configuration'"
echo "  4. Update Kiln/constitution/engineering.md if needed (tech stack, language rules)"
echo "  5. Run: ./Kiln.sh to launch the multi-agent session"

