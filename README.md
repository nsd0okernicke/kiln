# Kiln

**An orchestration platform that turns swarms of AI agents into reliable, professional software engineers.**

Kiln launches a config-driven multi-agent swarm, each agent working in its own git worktree with role-specific instructions and cross-agent communication. Works on Windows (PowerShell + WezTerm / Windows Terminal), macOS/Linux (zsh + tmux), and is Git-aware: sub-branches are created per worktree, all state lives in `.kiln/`, and handoffs are tracked in `logbook.md`.

---

## Quick Start

The fastest way to get a working Kiln project: run the install script.

### 1. Create a New Project

**Windows (PowerShell):**
```powershell
.\bin\kiln-init.ps1 -Target C:\path\to\my-project
cd C:\path\to\my-project
```

**Unix/macOS:**
```bash
./bin/kiln-init.sh /path/to/my-project
cd /path/to/my-project
```

This scaffolds a complete Kiln project with configuration, role files, and git initialization.

### 2. (Optional) Use an Example

Include an example project brief by adding the `-Example` flag:

**Windows:**
```powershell
.\bin\kiln-init.ps1 -Target C:\path\to\library-hub -Example library-hub
```

**Unix/macOS:**
```bash
./bin/kiln-init.sh /path/to/library-hub --example library-hub
```

### 3. Launch the Swarm

**Windows:**
```powershell
.\bin\kiln.ps1 -WorkingDir .
```

**Unix/macOS:**
```bash
./bin/kiln.sh .
```

That's it. Kiln will create git worktrees, generate role files, and launch agents in your configured terminal.

See **"Running Kiln"** below for more options and customization.

---

## What Kiln Does

Kiln is a lightweight orchestration layer that:

- Launches a **config-driven swarm** — specify each agent's role, AI tool/backend (claude/copilot/codex/grok), and workspace (main directory or isolated worktree)
  - Uses framework defaults from `kiln/profiles.json`
  - Projects can override by creating `kiln.profiles.json` at the root
  - Flexible terminal layouts: tabs, split panes, grids, or custom hierarchical arrangements
- Creates one **terminal window/tab per role** — observe all agents in real time
  - Windows: WezTerm or Windows Terminal tabs/panes (WezTerm preferred)
  - Unix/macOS: tmux sessions + Terminal.app or WezTerm
- Reads role behavior from `kiln/roles/<role>.md` files and a layered `kiln/constitution/` (workflow, engineering, project)
- Creates one **git worktree per agent** (except those using `@current`) under `.worktrees/` so agents don't collide — agents using `@current` work in the project root on the current branch
- Supports per-role **agent backends**: `claude`, `copilot`, `codex`, or `grok` — configure via `agent` field in profiles
- Creates **inter-agent messaging** via SQLite at `.kiln/messages.db`, exposed through two MCP servers:
  - **`kiln-db`** — SQL read/write for sending handoffs (`write_query`)
  - **`kiln-channel`** — blocking `wait_for_message()` tool that each agent calls to receive its next handoff; the channel polls the database and returns as soon as a message arrives, already marked delivered
- Keeps all swarm state in `.kiln/` (logs, sessions, message database) — gitignored and ephemeral

### Project Structure Created by `kiln-init`

When you run `kiln-init`, it scaffolds a new Kiln project with:

```text
my-project/
├── kiln/                         # Kiln configuration (version-controlled)
│   ├── constitution/
│   │   ├── workflow.md           # Handoff protocol
│   │   ├── engineering.md        # Engineering practices & quality standards
│   │   └── project.md            # Project-specific rules (language, architecture, constraints)
│   ├── roles/                    # Role definitions for your agents
│   │   ├── specifier.md
│   │   ├── coder.md
│   │   ├── refactorer.md
│   │   ├── architect.md
│   │   └── ...
│   └── skills/                   # Optional: custom agent skills
├── .kiln/                        # Runtime state (ephemeral, gitignored)
│   ├── messages.db              # SQLite message queue
│   ├── logs/                    # Agent logs
│   └── ...
├── .worktrees/                   # Git worktrees (gitignored)
│   ├── coder/
│   ├── refactorer/
│   ├── architect/
│   └── ...
├── .claude/                      # Claude Code configuration
│   ├── settings.json             # MCP and permission settings
│   └── .gitignore
├── .mcp.json                     # MCP server configuration (for Copilot agents)
├── .gitignore                    # Git exclusions
└── README.md                     # Project brief (optional, from example)
```

**Key points:**
- `kiln/` is version-controlled (constitution, roles, skills)
- `.kiln/` and `.worktrees/` are runtime/ephemeral (gitignored)
- Profiles are inherited from the framework; create `kiln.profiles.json` at the root to override

---

## Platform Support

### Windows (Native PowerShell)

Kiln has full native Windows support using PowerShell 7+ and WezTerm (or Windows Terminal).

```powershell
.\kiln.ps1 -WorkingDir "C:\path\to\project"
```

**Requirements:**
- PowerShell 7+ (included with Windows 11)
- WezTerm (recommended) or Windows Terminal (Microsoft Store)
- One or more agent CLIs (Claude Code, GitHub Copilot, Codex, or Grok) depending on configured agents

**Optional parameters:**
- `-Terminal wt` — use Windows Terminal instead of WezTerm
- `-Terminal wezterm` — explicitly use WezTerm (default when available)

### Unix/Linux/macOS (zsh + tmux)

Kiln on Unix uses zsh scripts and tmux for session management.

```sh
./kiln.sh <working-directory>
```

**Requirements:**

- zsh shell
- tmux for session management
- Python 3 with PyYAML module (used for YAML profile parsing)

  ```bash
  pip install pyyaml
  ```

- One or more agent CLIs (Claude Code, GitHub Copilot, Codex, or Grok) depending on configured agents
- Optional: WezTerm or Terminal.app (auto-detected)

---

## Framework Structure

The Kiln repository is organized for clarity and maintainability:

```text
kiln/
├── bin/                          # User-facing scripts
│   ├── kiln.sh             # Main launcher (Unix/macOS)
│   ├── kiln.ps1            # Main launcher (Windows)
│   ├── kiln-init.sh            # Project scaffolding (Unix/macOS)
│   ├── kiln-init.ps1           # Project scaffolding (Windows)
│   ├── kiln-cleanup.sh          # Manual cleanup (Unix/macOS)
│   ├── kiln-cleanup.ps1         # Manual cleanup (Windows)
│   ├── clear-messages.sh         # Clear message queue (testing utility)
│   └── clear-messages.ps1        # Clear message queue (testing utility)
│
├── lib/                          # Framework internals
│   ├── profile-loader.sh         # JSON profile parsing (Unix)
│   ├── profile-loader.ps1        # JSON profile parsing (PowerShell)
│   ├── terminal-adapter.sh       # Terminal backend loader (Unix)
│   ├── terminal-adapters/        # Terminal backend implementations
│   │   ├── wezterm.ps1           # WezTerm adapter (Windows)
│   │   ├── wezterm.sh            # WezTerm adapter (Unix)
│   │   ├── windows-terminal.sh   # Windows Terminal (WSL)
│   │   ├── terminal-app.sh       # macOS Terminal.app
│   │   ├── ghostty.sh            # Ghostty terminal
│   │   └── none.sh               # Fallback (current shell)
│   └── kiln-window-watchdog.sh   # Window tracking (Unix tmux)
│
├── kiln/                   # Master framework templates & default profiles
│   ├── profiles.json             # Default configuration profiles (framework defaults only)
│   ├── constitution/             # Shared constitution rules (copied to projects)
│   │   ├── workflow.md           # Handoff protocol
│   │   ├── engineering.md        # Engineering practices & quality standards
│   │   └── project.md            # Project rules starter template (fill in language, constraints)
│   ├── roles/                    # Role prompts (copied to projects)
│   ├── mcp-server/               # Python MCP servers bundled with the framework
│   │   ├── channel.py            # kiln-channel: blocking wait_for_message() receiver
│   │   └── requirements.txt      # mcp>=1.0.0
│   └── skills/                   # Agent skills (optional, copied to projects)
│
├── examples/                     # Example project briefs
│   └── library-hub/README.md     # LibraryHub reference example
│
├── tests/                        # Framework tests
└── docs/                         # Documentation & assets
```

**User Scripts** (`bin/`) are the entry points for Kiln operations. **Framework Internals** (`lib/`) are implementation details — developers shouldn't need to modify them. **Templates** (`kiln/`) are copied to new projects during scaffolding.

---

## Core Features

- **Config-Driven Topology** — The swarm shape comes from `kiln/profiles.json`, not hardcoded variables.
- **Flexible Terminal Layouts** — Define custom tab and pane arrangements in your profile: simple tabs, split panes, 2×2 grids, hierarchical trees, or focus layouts (e.g., 1 full tab + 3-way split below).
- **Role Injection** — Constitution (`workflow.md`, `engineering.md`, `project.md`) and role instructions (`roles/<role>.md`) are merged into each agent's instruction file (`CLAUDE.md` or `.github/copilot-instructions.md`), giving full context immediately.
- **Project-Local Constitution** — Customize architecture, tech stack, and quality gates via `kiln/constitution/project.md`.
- **Layered Rules** — `kiln/constitution/` contains `workflow.md` (handoffs), `engineering.md` (tools/practices), and `project.md` (arch/quality) — all applied to every agent.
- **Backend Selection Per Role** — Each role can launch `claude`, `copilot`, `codex`, or `grok` via the `agent` field in profiles.
- **Observable Swarm** — Watch all agents in one window (tabs or panes on Windows, tmux panes on Unix).
- **Cross-Platform** — Works on Windows, macOS, and Linux with zero duplication.

---

## Constitution and Roles

The recommended project layout is:

```text
kiln/
  roles/
    architect.md             # Architect role (design review, approval)
    coder.md                 # Coder role (TDD implementation)
    refactorer.md            # Refactorer role (quality gates, refactoring)
    specifier.md             # Specifier role (Gherkin acceptance tests)
    reviewer.md              # Reviewer role (batch review alternative to refactorer)
    selftest.md              # Selftest role (communication chain validation)
  constitution/
    workflow.md              # Handoff protocol, branch discipline, queue format
    engineering.md           # Language, tools, dependencies, practices
    project.md               # Project-specific architecture, tech stack, quality gates

# Optional: Override default profiles (framework uses kiln/profiles.json)
kiln.profiles.json           # Project-specific profiles (optional, at root)
```

**Note:** Configuration profiles are inherited from the framework default (`kiln/profiles.yaml`). Projects can optionally override by creating `kiln.profiles.yaml` at the project root if they need custom profile definitions.

### Profile Loading & Inheritance

Configuration profiles define which agents run, which roles they take, and where they work. Kiln uses a cascading search to find profiles:

1. **Project root** (`kiln.profiles.json`) — Project-level overrides
2. **Project config** (`kiln/profiles.json`) — Not used (projects don't copy profiles)
3. **Project state** (`.kiln/profiles.json`) — Not used
4. **Framework** (`kiln/profiles.json`) — Default profiles for all projects
5. **User home** (`~/.kiln/profiles.json`) — User-level defaults (optional)
6. **System** (`/etc/kiln/profiles.json`) — System-wide defaults (optional)

By default, **all projects use the framework's `kiln/profiles.json`**, which defines the standard 4-agent workflow (specifier, coder, refactorer, architect). This means new projects work immediately without configuration.

**To customize profiles for a specific project**, create `kiln.profiles.json` at the project root. Kiln will use your custom profiles instead of the framework defaults.

### Layered Constitution

- **`constitution/workflow.md`** — Defines handoff protocol, git worktree discipline, and cross-agent communication rules.
- **`constitution/engineering.md`** — Specifies language, build tools, test frameworks, quality tools, and coding practices.
- **`constitution/project.md`** — Project-specific rules: language, architecture constraints, quality thresholds. Initialized from the framework starter template; fill in or extend for your project. Example projects (like library-hub) keep detailed technical rules in their `README.md` so agents get them as part of the project brief.

**Agent Instruction Assembly:** Constitution and role instructions are **always combined** at startup:

- Constitution files (`workflow.md`, `engineering.md`, `project.md`) provide shared rules and context for all agents
- Role file (`roles/<role>.md`) provides role-specific instructions and behavior
- Both are merged into each agent's generated instruction file:
  - **Claude agents**: `CLAUDE.md` in the worktree root
  - **Copilot agents**: `.github/copilot-instructions.md` in the worktree root
  - **Other backends**: Similar instruction file per backend

This ensures every agent operates with full constitutional context plus its specific role directives.

### Default Workflow

The default four-agent workflow runs in a continuous loop. Each agent (except specifier) follows the same **Message Loop**:

1. **Wait** — call `wait_for_message()` via the `kiln-channel` MCP server (blocks until a handoff arrives)
2. **Merge** — `git merge <commit>` from the sender's branch into their own
3. **Log received** — write a logbook.md entry
4. **Work** — role-specific task (see below)
5. **Log sent** — write a logbook.md entry for the outgoing handoff
6. **Squash** — squash work commits into one
7. **Send handoff** — INSERT into `.kiln/messages.db` via `write_query`
8. Return to step 1

The cycle flows: **specifier → coder → refactorer → architect → specifier**

- **`specifier`** — At startup, asks the user what feature to specify. Writes Gherkin acceptance tests, gets user approval, sends handoff to coder. After sending, calls `wait_for_message()` to wait for architect's completion signal before starting the next feature.
- **`coder`** — Implements behavior slices using strict TDD until all tests pass, then sends handoff to refactorer. The loop is not complete until the handoff is sent.
- **`refactorer`** — Runs quality gates (coverage → CRAP → DRY → mutation site count), refactors for testability, sends handoff to architect.
- **`architect`** — Reviews module structure, runs pre-handoff verification (mutation → DRY → soft Gherkin), sends "The job is complete" to specifier.

> **Optional role:** `reviewer` is an alternative to `refactorer` with a focus on batch processing and review pipelines. Add it to your profile in `kiln/profiles.json` to use it instead. See `kiln/roles/reviewer.md`.

---

## Running Kiln

### Quick Reference

| Platform | Command | Options |
|---|---|---|
| **Windows** | `.\kiln.ps1 -WorkingDir .` | `-ProfileName <profile>` for different profiles; `-Terminal wt` to use Windows Terminal; `-Debug` for verbose output |
| **Unix/macOS** | `./kiln.sh .` | `--profile <profile>` for different profiles; Terminal auto-detected: WezTerm > Terminal.app > tmux |

Kiln will create a git repository if one doesn't exist, initialize worktrees, and launch agents.

### Windows (PowerShell)

1. **Create a new project** from the Kiln repository root:

   ```powershell
   .\bin\kiln-init.ps1 -Target C:\path\to\my-project
   cd C:\path\to\my-project
   ```

   This scaffolds the project with all necessary files: constitution, roles, tools, and git initialization.

2. **Optional: Include an example brief** (library-hub):

   ```powershell
   .\bin\kiln-init.ps1 -Target C:\path\to\library-hub -Example library-hub
   ```

   This adds the example README.md as your project brief so agents immediately know what to build.

3. **Run Kiln**:

   ```powershell
   .\bin\kiln.ps1 -WorkingDir .
   ```

   Optional profile and terminal control:

   ```powershell
   # Run the default 'dev' profile (standard 4-agent swarm with tab layout)
   .\bin\kiln.ps1 -WorkingDir .

   # Run a different profile (e.g., 'compact' with different layout or agent configuration)
   .\bin\kiln.ps1 -WorkingDir . -ProfileName compact

   # Use Windows Terminal instead of WezTerm (default)
   .\bin\kiln.ps1 -WorkingDir . -Terminal wt

   # Enable debug mode (verbose output for troubleshooting MCP issues)
   .\bin\kiln.ps1 -WorkingDir . -Debug

   # Kill orphaned MCP server processes after closing the terminal
   .\bin\kiln.ps1 -Stop
   ```

4. **Startup creates**:
   - Git worktrees under `.worktrees/` (one per non-@current role)
   - Generated `CLAUDE.md` files in each worktree with embedded constitution + project + role content
   - Per-worktree `.mcp.json` with both `kiln-db` and `kiln-channel` configured (correct role and branch env vars injected)
   - Channel log files at `.kiln/logs/channel-<role>.log` for debugging
   - WezTerm tabs/panes (or Windows Terminal tabs) for each role
   - `.kiln/messages.db` SQLite database for inter-agent messaging via MCP

5. **Verify**: Each agent's tab shows a prompt. Ask it: `pwd` to confirm it's in the correct worktree.

### Unix/macOS (zsh)

1. **Create a new project** from the Kiln repository root:

   ```sh
   ./bin/kiln-init.sh /path/to/my-project
   cd /path/to/my-project
   ```

   This scaffolds the project with all necessary files: constitution, roles, tools, and git initialization.

2. **Optional: Include an example brief** (library-hub):

   ```sh
   ./bin/kiln-init.sh /path/to/library-hub --example library-hub
   ```

   This adds the example README.md as your project brief so agents immediately know what to build.

3. **Run Kiln**:

   ```sh
   ./bin/kiln.sh .
   ```

4. **What happens**:
   - Creates git worktrees under `.worktrees/`
   - Launches tmux sessions (one per role)
   - Creates Terminal.app windows or WezTerm tabs (auto-detected)
   - Each agent gets a tmux pane to run in
   - Generates `CLAUDE.md` files with full constitution and role content

---

## Configuration Profiles

Kiln uses JSON profiles to define swarm topology. The default profile is `dev`, which creates the standard 4-agent swarm. All projects inherit the framework's default profiles from `kiln/profiles.json` automatically.

**To customize profiles for a specific project**, create `kiln.profiles.json` at your project root. Kiln will use your custom profiles instead of the framework defaults.

### Framework Default Profile

The framework provides the standard `dev` profile with tab-based layout:

```json
{
  "profiles": {
    "dev": {
      "description": "Standard 4-agent swarm with isolated worktrees",
      "terminals": [
        {
          "role": "specifier",
          "agent": "copilot",
          "worktree": "@current",
          "model": "claude-haiku-4-5-20251001"
        },
        {
          "role": "coder",
          "agent": "claude",
          "worktree": "coder",
          "model": "claude-haiku-4-5-20251001"
        },
        {
          "role": "refactorer",
          "agent": "claude",
          "worktree": "refactorer",
          "model": "claude-haiku-4-5-20251001"
        },
        {
          "role": "architect",
          "agent": "claude",
          "worktree": "architect",
          "model": "claude-haiku-4-5-20251001"
        }
      ],
      "layout": {
        "type": "tabs",
        "roles": ["specifier", "coder", "refactorer", "architect"]
      }
    }
  }
}
```

**Terminal fields:**

- **role** — maps to `kiln/roles/<role>.md` (must exist)
- **agent** — which AI tool to use: `claude`, `copilot`, `codex`, or `grok`
- **worktree** — `@current` to work in the main directory, or any name (creates `.worktrees/<name>/`)
  - Use `@current` for coordinator/review roles that work on the current branch
  - Use separate worktree names for roles that need isolation (e.g., each agent on its own branch)
- **model** — (Claude agents only) which Claude model to use, e.g., `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-opus-4-8`

### Layout Configurations

The `layout` field defines how agents are displayed in the terminal. Kiln supports multiple layout types:

**Tabs Layout** (default):

```json
"layout": {
  "type": "tabs",
  "roles": ["specifier", "coder", "refactorer", "architect"]
}
```

Each role gets its own tab.

**Grid Layout** (2×2, 3×3, etc.):

```json
"layout": {
  "tabs": [
    {
      "title": "All Roles",
      "gridRows": 2,
      "gridCols": 2,
      "panes": [
        {"role": "specifier"},
        {"role": "coder"},
        {"role": "refactorer"},
        {"role": "architect"}
      ]
    }
  ]
}
```

All agents visible simultaneously in a grid within a single tab.

**Split Panes Layout**:

```json
"layout": {
  "tabs": [
    {
      "panes": [
        {"role": "specifier"},
        {"role": "coder"}
      ]
    },
    {
      "panes": [
        {"role": "refactorer"},
        {"role": "architect"}
      ]
    }
  ]
}
```

Multiple tabs, each with side-by-side panes.

**Focus Layout** (1 top, multiple bottom):

```json
"layout": {
  "tabs": [
    {
      "panes": [{"role": "specifier"}]
    },
    {
      "panes": [
        {"role": "coder"},
        {"role": "refactorer"},
        {"role": "architect"}
      ]
    }
  ]
}
```

### Per-Role Agent Selection

You can mix different agents in a single swarm:

```json
{
  "profiles": {
    "mixed": {
      "description": "Swarm with different agent backends per role",
      "terminals": [
        {
          "role": "specifier",
          "agent": "copilot",
          "worktree": "@current"
        },
        {
          "role": "coder",
          "agent": "claude",
          "worktree": "coder",
          "model": "claude-sonnet-4-6"
        },
        {
          "role": "refactorer",
          "agent": "claude",
          "worktree": "refactorer",
          "model": "claude-haiku-4-5-20251001"
        },
        {
          "role": "architect",
          "agent": "grok",
          "worktree": "architect"
        }
      ],
      "layout": {
        "type": "tabs",
        "roles": ["specifier", "coder", "refactorer", "architect"]
      }
    }
  }
}
```

Each agent backend requires the corresponding CLI tool to be installed and available in `PATH`.

### Running a Different Profile

Launch a specific profile with the `-ProfileName` (Windows) or `--profile` (Unix) flag:

```powershell
# Windows
.\kiln.ps1 -WorkingDir . -ProfileName staging
```

```bash
# Unix/macOS
./kiln.sh . --profile staging
```

If no profile is specified, `dev` is used by default. The working directory argument is required.

### Gitflow-Aware Branch Naming

Sub-branches are named `<current-branch>-<worktreeName>` — Kiln automatically mirrors the active branch namespace. On `feature/ABC123` they become `feature/ABC123-coder`, `feature/ABC123-refactorer`, etc. On `main` they become `main-coder`, `main-refactorer`, etc.

**Roles with `@current` worktree** work directly on the current branch in the main project directory and do not create sub-branches.

Sub-branches are **local-only and cannot be pushed** — Kiln enforces this via a git pre-push hook. Attempting to push a sub-branch will fail. This is by design: sub-branches are orchestration-internal and ephemeral.

---

## Terminal Behavior

Kiln opens terminal windows or tabs through a small terminal backend adapter.

### Auto-Detection (Unix)

1. If `$WEZTERM_PANE` is set and `wezterm` is in PATH → WezTerm
2. If AppleScript is available → macOS Terminal.app
3. If `wt.exe` is available → Windows Terminal (from WSL)
4. Otherwise → attach the cleanup tmux session in the current shell

### Auto-Detection (Windows)

1. If `$env:WEZTERM_PANE` is set and `wezterm` is in PATH → WezTerm
2. If `wezterm` is available → WezTerm
3. If `wt.exe` is available → Windows Terminal
4. Otherwise → error

### Override the Default

Set `KILN_TERMINAL` to force a specific backend:

**Unix:**
```sh
KILN_TERMINAL=wezterm ./kiln.sh .
KILN_TERMINAL=none ./kiln.sh .
```

**Windows:**
```powershell
$env:KILN_TERMINAL = "wezterm"
.\kiln.ps1 -WorkingDir .
```

### Layout Examples

Kiln supports flexible layout configurations that can be defined in your profile. Layouts can range from simple (tabs) to complex (hierarchical splits, grids, focus layouts).

#### Supported Layout Types

**Tabs layout** (default):

- One terminal tab per agent (e.g., 4 agents = 4 tabs)
- Each tab runs independently with its own color scheme
- Clear visual separation of roles
- Easy to click between agents
- Use when: you want simple visual separation

**Grid layout** (2×2, 3×3, etc.):

- All agents visible simultaneously in one window
- Configurable rows and columns (e.g., `gridRows: 2, gridCols: 2`)
- Compact view; observe all roles at once
- Useful for rapid-fire coordination
- Use when: you want all agents visible and have few enough roles to fit in a grid

**Split panes layout**:

- Multiple tabs, each with their own pane arrangement
- E.g., Tab 1: specifier + coder side-by-side; Tab 2: refactorer + architect side-by-side
- Balances visibility and organization
- Use when: you want to group related agents together

**Focus layout** (top-focused):

- Top tab shows the current focus role at full height
- Bottom tab shows supporting roles in a multi-pane split
- E.g., specifier at top, coder/refactorer/architect split at bottom
- Use when: you want to focus on one agent while monitoring others

All layouts work on **WezTerm**, **Windows Terminal**, and **Unix/macOS tmux**.

### WezTerm Config Behavior

Kiln dynamically generates a WezTerm configuration file at runtime to set up the multi-agent layout (when WezTerm is used, which is the default on Windows). **Important:**

- When you run `kiln.ps1`, Kiln writes a generated `~/.wezterm.lua` file to your home directory
- This config is tailored to your specific agents and layout (tabs or panes)
- **Your existing `~/.wezterm.lua` is backed up** to `~/.wezterm.lua.kiln-backup` before writing
- **The backup is automatically restored** ~500ms after WezTerm starts (when Kiln detects the window has opened)
- Your personal config is preserved; Kiln's generated config is temporary and session-specific

**If something goes wrong:** If the restore fails or you need to manually restore your config, run:

```powershell
Move-Item ~/.wezterm.lua.kiln-backup ~/.wezterm.lua -Force
```

### tmux Behavior (Unix Only)

Kiln uses a project-specific tmux socket (recorded in `.kiln/tmux-socket`), so each project's swarm is isolated from other tmux sessions. It honors tmux `base-index` and `pane-base-index` settings when launching agents, so configurations that number windows from `1` work without requiring users to change their tmux preferences.

When Kiln opens trackable terminal windows or tabs, it starts a small watchdog:

- Closing a non-cleanup terminal surface reopens that surface attached to the same tmux session.
- Closing the cleanup terminal surface shuts down all configured tmux sessions and closes the remaining tracked surfaces.

### Adding A Terminal Backend

Terminal backends live in `lib/terminal-adapters/`. To add a new backend, create one file named after the backend:

```text
lib/terminal-adapters/wezterm.sh
```

or

```text
lib/terminal-adapters/wezterm.ps1
```

The file must define this contract (Unix shell example):

```sh
terminal_backend_label() {
  echo "WezTerm"
}

terminal_backend_can_open_sessions() {
  return 0
}

terminal_backend_tracks_windows() {
  return 0
}

terminal_open_session() {
  local session="$1"
  local title="$2"
  local sibling_id="${3:-}"
  # Open a terminal surface and print its stable id
}

terminal_window_exists() {
  local window_id="$1"
  # Return 0 if still exists, nonzero otherwise
}

terminal_close_window() {
  local window_id="$1"
  # Close the window
}
```

---

## Cleanup

After a Kiln session completes, you can optionally clean up swarm artifacts from your project:

**Windows:**
```powershell
.\bin\kiln-cleanup.ps1 -ProjectDir <path-to-project>
```

**Unix/macOS:**
```bash
./bin/kiln-cleanup.sh <path-to-project>
```

The cleanup script removes:
- Git worktrees (`.worktrees/`) and associated branches
- Swarm state (`.kiln/`)
- Generated instruction files (`CLAUDE.md`, `.github/copilot-instructions.md`)
- Git hooks installed for swarm discipline
- Terminal window/tab records

**Note:** Cleanup is **optional and manual** — it only runs when you explicitly call it. This gives you full control and the ability to inspect or debug your project state before cleaning up.

---

## Examples

The repository includes example project briefs under `examples/`. These are intended to be used with the install scripts.

- `examples/library-hub/README.md` — LibraryHub, a FastAPI microservices project with hexagonal architecture, RabbitMQ event-driven communication, and full TDD/mutation-testing quality gates. Includes architecture & layering rules, tech stack, quality gates, and testing strategy — all as part of the project brief so agents have complete technical context. This serves as the reference implementation for Kiln.

To scaffold a new LibraryHub project:

**Windows:**
```powershell
.\bin\kiln-init.ps1 -Target C:\my-library-hub -Example library-hub
```

**Unix/macOS:**
```bash
./bin/kiln-init.sh /path/to/my-library-hub --example library-hub
```

This creates a complete, ready-to-run project with the LibraryHub brief included.

---

## Communication System Health Check (Self-Test)

After launching Kiln, you can verify that inter-agent communication is working by running the built-in self-test.

### Setup

Create a `selftest` profile in `kiln/profiles.json` with the `selftest` role as the **first entry**:

```json
{
  "profiles": {
    "selftest": {
      "description": "Communication chain test with selftest agent",
      "terminals": [
        {"role": "selftest", "agent": "claude", "worktree": "@current"},
        {"role": "coder", "agent": "claude", "worktree": "coder"},
        {"role": "refactorer", "agent": "claude", "worktree": "refactorer"},
        {"role": "architect", "agent": "claude", "worktree": "architect"}
      ],
      "layout": {
        "type": "tabs",
        "roles": ["selftest", "coder", "refactorer", "architect"]
      }
    }
  }
}
```

Then launch with:

```sh
./kiln.sh --profile selftest
```

The selftest must be first because it acts as the test initiator and receiver for the communication chain.

### Running the Test

Once all agents are launched:

1. **Agents block on `wait_for_message()`**: Each agent calls the `kiln-channel` MCP server at startup and blocks until a message arrives. No manual prompting needed.

2. **In the selftest window**, paste this prompt to initiate the chain:
   ```
   I am running the selftest prompt. Begin the communication chain test now.
   ```

3. **The chain executes automatically**:
   - Selftest sends a test message to coder via `write_query` SQL INSERT
   - Each agent's `wait_for_message()` call returns when the message arrives (already marked delivered)
   - Agents detect the "system-communication-test" marker and forward as-is (test pass-through, no actual work)
   - The final agent (architect) sends completion back to selftest
   - Selftest receives completion and reports success

4. **Monitor channel logs** at `.kiln/logs/channel-<role>.log` to see polling activity per agent.

### Expected Output

```
══════════════════════════════════════════════════════════════
✓ Kiln COMMUNICATION TEST: PASSED
══════════════════════════════════════════════════════════════

Role:              selftest
Configuration:     4 agents configured
Chain:             selftest → coder → refactorer → architect → selftest
Test-ID:           selftest-20260608-143022
Duration:          45 seconds

✓ All messages queued correctly in SQLite database
✓ All agents processed messages and updated logbook.md
✓ MCP SQLite messaging operational
✓ All worktrees accessible
✓ MCP tools available on each agent

Review logbook.md for complete chain trace.

══════════════════════════════════════════════════════════════
```

### What It Tests

- **Agent discovery**: Each agent can locate its role and configuration
- **Message delivery**: Messages are correctly queued in SQLite database (`.kiln/messages.db`)
- **Message status lifecycle**: Messages progress through queued → delivered → processed states
- **Priority ordering**: High-priority messages are delivered before normal messages
- **Logbook tracking**: Each agent writes handoff entries to `logbook.md`
- **Cross-platform**: Works on Windows (PowerShell) and Unix/macOS (bash/zsh)

### Inspection

After the test completes:

```bash
# View message database stats (shows queued/delivered/processed counts)
sqlite3 .kiln/messages.db "SELECT status, COUNT(*) as count FROM messages GROUP BY status;"

# View logbook trace of the entire chain
git log -p logbook.md | grep -A5 SELFTEST

# Verify all messages had unique test IDs
grep "selftest-" logbook.md

# Inspect a specific message in the database
sqlite3 .kiln/messages.db "SELECT id, sender, target, priority, status, content FROM messages WHERE sender = 'selftest' LIMIT 1;"
```

### Troubleshooting

If the test hangs or fails:

1. **Check agent status**: Make sure all configured agents are running
2. **Verify database exists**: `ls .kiln/messages.db` — should be created at startup
3. **Check MCP configuration**: Verify `.mcp.json` is present in the project Kiln directory
4. **Review agent console**: Each agent window shows what it received and did
5. **Check logbook.md**: Look for error messages or incomplete entries
6. **Query database directly**: `sqlite3 .kiln/messages.db "SELECT COUNT(*) FROM messages;"` to verify messages were inserted
7. **Check agent permissions**: Ensure agents have MCP tool permissions in `.claude/settings.json`

---

## Project Maturity & Status

**Kiln v0.1 — PHASE 4: CHANNEL-BASED MESSAGING ✓ COMPLETE**

### ✓ Completed Features

- **Phase 1: Framework Architecture** — Config-driven swarm orchestration, role injection, git worktree isolation
- **Phase 2: Cross-Platform Infrastructure** — Windows (PowerShell/Windows Terminal/WezTerm), Unix/macOS (zsh/tmux)
- **Phase 3: Auto-Agent Communication** — SQLite message queues with MCP server, automated role-based message forwarding, full agent chain test passing
- **Phase 4: Channel-Based Messaging** — Replaced SQL inbox polling with a blocking `wait_for_message()` Channel
  - ✓ `kiln-channel` Python MCP server (`kiln/mcp-server/channel.py`) — polls SQLite and blocks until a message arrives, returns it already marked delivered
  - ✓ Per-worktree `.mcp.json` generated with `kiln-db` + `kiln-channel`, correct `KILN_ROLE`/`KILN_BRANCH` env vars injected per agent
  - ✓ Explicit numbered Message Loop in every role: wait → merge → log → work → log → squash → handoff → repeat
  - ✓ Channel debug logs at `.kiln/logs/channel-<role>.log`
  - ✓ `-Stop` flag on `kiln.ps1` to kill orphaned MCP server processes after terminal close

### Current Capabilities

- ✓ Multi-agent swarms (2-5 agents typical)
- ✓ Per-role configuration and role injection
- ✓ Isolated git worktrees with branch naming (e.g., `feature/ABC-coder`, `main-refactorer`)
- ✓ Blocking Channel messaging — agents call `wait_for_message()` and are notified the instant a handoff arrives
- ✓ SQL handoff sending via MCP `kiln-db` `write_query`
- ✓ Layered constitution system (workflow, engineering, project)
- ✓ Cross-platform terminal support (Windows Terminal, WezTerm, tmux)
- ✓ Flexible terminal layouts (tabs, split panes, grids, focus layouts)
- ✓ Per-agent model configuration for Claude agents
- ✓ Built-in communication health check (selftest agent)
- ✓ Logbook tracking of all handoffs and agent actions

### ⚠️ Security Considerations

**Agent Permissions:** Kiln agents run with **full permission rights by default** to enable seamless autonomous operation:

- **Claude agents**: `--permission-mode bypassPermissions` (auto-approve all MCP tools and file operations)
- **Copilot agents**: `--allow-all` (auto-approve GitHub Copilot tools and file access)
- **Codex agents**: Similar permission bypass mechanism (TBD in implementation)
- **Grok agents**: Similar permission bypass mechanism (TBD in implementation)

This means agents can read/write/execute any file in their worktree without prompting. This is intentional for autonomous development workflows but should be understood as a security trade-off.

**Risk mitigation:**

- Keep Kiln projects in isolated, non-production directories
- Do not run agents with sensitive data (credentials, secrets, PII) in the project
- Use git worktrees for isolation — agents can only access their assigned worktree and shared `.kiln/` directory
- Review agent outputs and commits before merging to main branch
- Consider running Kiln in a sandbox/VM for untrusted code or high-security scenarios

### Known Limitations & Future Work

- **Real feature workflows not yet tested** — Phase 3 validates infrastructure; actual multi-agent feature development (specifying → coding → refactoring → verification) requires additional testing
- **Error handling** — Minimal error recovery in agent workflows; graceful degradation not yet implemented
- **Scaling** — Tested with 4-5 agents; behavior with 10+ agents unknown
- **Multi-agent backend validation** — Framework supports `claude` and `copilot` (validated); `codex` and `grok` support planned but not yet implemented

### Recommended Next Steps

1. **Run real feature workflows** — Test specifier → coder → refactorer → architect chain with actual code implementation
2. **Add error handling** — Implement graceful failure modes when agents can't process messages
3. **Multi-language projects** — Test with Python, Kotlin, JavaScript projects beyond the LibraryHub FastAPI example
4. **CI/CD integration** — Determine how to integrate Kiln agents into GitHub Actions / GitLab CI workflows

---

## Acknowledgments

Kiln was inspired by [Uncle Bob's swarm-forge](https://github.com/unclebob/swarm-forge), a framework for multi-agent development. While taking cues from that design philosophy, Kiln evolves the concept with a focus on TDD-driven workflows, MCP messaging standards, and production-ready orchestration for AI agents across multiple languages and platforms.


