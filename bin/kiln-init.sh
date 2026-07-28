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
    get_available_profiles "$FRAMEWORK_ROOT/kiln/framework"
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
mkdir -p "$TARGET/kiln/project/constitution"
mkdir -p "$TARGET/kiln/project/roles"
mkdir -p "$TARGET/kiln/project/skills"
mkdir -p "$TARGET/.kiln"
echo "✓ Created directory structure"

# Copy constitution files
for file in engineering.md workflow.md project.md; do
    if [ -f "$KILN_DIR/project/constitution/$file" ]; then
        cp "$KILN_DIR/project/constitution/$file" "$TARGET/kiln/project/constitution/"
    fi
done
echo "✓ Copied constitution files"

# Copy role files
if [ -d "$KILN_DIR/project/roles" ]; then
    for file in "$KILN_DIR/project/roles"/*.md; do
        if [ -f "$file" ]; then
            cp "$file" "$TARGET/kiln/project/roles/"
        fi
    done
    echo "✓ Copied role files"
fi

# Copy skills directory
if [ -d "$KILN_DIR/project/skills" ]; then
    for skill_dir in "$KILN_DIR/project/skills"/*; do
        if [ -d "$skill_dir" ]; then
            cp -r "$skill_dir" "$TARGET/kiln/project/skills/"
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


# project.md is copied above along with engineering.md/workflow.md — this used to be an
# independently hardcoded heredoc here (out of sync with both the framework's real project.md
# and with kiln-init.ps1's starter content) — now there's one source of truth.

# Copy the framework's real constitution.md instead of synthesizing our own — this used to be
# an independently hardcoded heredoc (with a stale `Kiln/`-capitalized path bug) that could
# drift from the framework's actual file. Now there's one source of truth.
if [ -f "$KILN_DIR/project/constitution.md" ]; then
    cp "$KILN_DIR/project/constitution.md" "$TARGET/kiln/project/constitution.md"
    echo "✓ Copied constitution.md"
fi


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
    example_project_md="$FRAMEWORK_ROOT/examples/$EXAMPLE/kiln/project/constitution/project.md"
    if [ -f "$example_project_md" ]; then
        cp "$example_project_md" "$TARGET/kiln/project/constitution/project.md"
        echo "✓ Copied example-specific project.md"
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
echo "  2. Review kiln/project/constitution/ and kiln/project/roles/"
echo "  3. git add kiln/ .claude/ && git commit -m 'Add Kiln configuration'"
echo "  4. Update kiln/project/constitution/engineering.md if needed (tech stack, language rules)"
echo "  5. Run: ./kiln.sh to launch the multi-agent session"

