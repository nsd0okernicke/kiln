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
  - Uses framework defaults from `kiln/framework/profiles.json`
  - Projects can override by creating `kiln.profiles.json` at the root
  - Flexible terminal layouts: tabs, split panes, grids, or custom hierarchical arrangements
- Creates one **terminal window/tab per role** — observe all agents in real time
  - Windows: WezTerm or Windows Terminal tabs/panes (WezTerm preferred)
  - Unix/macOS: tmux sessions + Terminal.app or WezTerm
- Reads role behavior from `kiln/project/roles/<role>.md` files and a layered `kiln/project/constitution/` (workflow, engineering, project)
- Creates one **git worktree per agent** (except those using `@current`) under `.worktrees/` so agents don't collide — agents using `@current` work in the project root on the current branch
- Supports per-role **agent backends**: `claude`, `copilot`, `codex`, or `grok` — configure via `agent` field in profiles
- Creates **inter-agent messaging** via SQLite at `.kiln/messages.db` with full message lifecycle tracking, exposed through two MCP servers:
  - **`kiln-db`** — SQL read/write for sending handoffs (`write_query`)
  - **`kiln-channel`** — blocking `wait_for_message()` tool that each agent calls to receive its next handoff; the channel polls the database and returns as soon as a message arrives, already marked `delivered`. Also provides `mark_processing()` and `mark_processed()` to transition messages through their full lifecycle
  - **Message lifecycle**: `queued` (created) → `delivered` (retrieved by agent) → `processing` (work started) → `processed` (handoff sent)
- Keeps all swarm state in `.kiln/` (logs, sessions, message database) — gitignored and ephemeral

### Project Structure Created by `kiln-init`

When you run `kiln-init`, it scaffolds a new Kiln project with:

```text
my-project/
├── kiln/                         # Kiln configuration (version-controlled)
│   └── project/                  # Everything here is yours to customize — see kiln/project/README.md
│       ├── constitution.md
│       ├── constitution/
│       │   ├── workflow.md           # Handoff protocol
│       │   ├── engineering.md        # Engineering practices & quality standards
│       │   └── project.md            # Project-specific rules (language, architecture, constraints)
│       ├── roles/                    # Role definitions for your agents
│       │   ├── specifier.md
│       │   ├── coder.md
│       │   ├── refactorer.md
│       │   ├── architect.md
│       │   └── ...
│       └── skills/                   # Optional: custom agent skills
├── .kiln/                        # Runtime state (ephemeral, gitignored)
│   ├── messages.db              # SQLite message queue
│   ├── logs/                    # Agent logs
│   ├── status/                  # Live per-role state (<role>.json), read by the WezTerm status bar
│   └── ...
├── .worktrees/                   # Git worktrees (gitignored)
│   ├── coder/
│   ├── refactorer/
│   ├── architect/
│   └── ...
├── .claude/                      # Claude Code configuration
│   ├── settings.json             # MCP and permission settings
│   └── .gitignore
├── .mcp.json                     # MCP server configuration (kiln-db + kiln-channel, for Claude agents)
├── .gitignore                    # Git exclusions
└── README.md                     # Project brief (optional, from example)
```

**Key points:**
- `kiln/project/` is version-controlled (constitution, roles, skills) — customize freely, this is your project's own copy
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
│   ├── clear-messages.ps1        # Clear message queue (testing utility)
│   └── kiln-db.ps1               # Inspect/manage messages.db (Windows only, no Unix equivalent yet)
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
│   ├── project/                  # Copied into every new project's kiln/project/ — customize freely
│   │   ├── constitution.md
│   │   ├── constitution/         # Shared constitution rules
│   │   │   ├── workflow.md           # Handoff protocol
│   │   │   ├── engineering.md        # Engineering practices & quality standards
│   │   │   └── project.md            # Project rules starter template (fill in language, constraints)
│   │   ├── roles/                    # Role prompts
│   │   └── skills/                   # Agent skills (optional)
│   └── framework/                # Never copied — read directly from this install
│       ├── profiles.json             # Default configuration profiles (framework defaults only)
│       ├── templates/                # Loop/runtime templates injected into generated CLAUDE.md/copilot-instructions.md/AGENTS.md
│       ├── mcp-server/               # Python MCP servers bundled with the framework
│       │   ├── channel.py            # kiln-channel: blocking wait_for_message() receiver
│       │   └── requirements.txt      # mcp>=1.0.0
│       └── tools/                    # set-status.py — re-seeded into .kiln/tools/ on every launch
│
├── examples/                     # Example project briefs
│   └── library-hub/README.md     # LibraryHub reference example
│
├── tests/                        # Framework tests
└── docs/                         # Documentation & assets
```

**User Scripts** (`bin/`) are the entry points for Kiln operations. **Framework Internals** (`lib/`) are implementation details — developers shouldn't need to modify them. **`kiln/project/`** is copied to new projects during scaffolding and is meant to be customized. **`kiln/framework/`** is never copied — it's read directly from this install at generation/launch time, so edits there affect every project using this install.

---

## Core Features

- **Config-Driven Topology** — The swarm shape comes from `kiln/framework/profiles.json`, not hardcoded variables.
- **Flexible Terminal Layouts** — Define custom tab and pane arrangements in your profile: simple tabs, split panes, 2×2 grids, hierarchical trees, or focus layouts (e.g., 1 full tab + 3-way split below).
- **Role Injection** — Constitution (`workflow.md`, `engineering.md`, `project.md`) and role instructions (`roles/<role>.md`) are merged into each agent's instruction file (`CLAUDE.md` or `.github/copilot-instructions.md`), giving full context immediately.
- **Project-Local Constitution** — Customize architecture, tech stack, and quality gates via `kiln/project/constitution/project.md`.
- **Layered Rules** — `kiln/project/constitution/` contains `workflow.md` (handoffs), `engineering.md` (tools/practices), and `project.md` (arch/quality) — all applied to every agent.
- **Backend Selection Per Role** — Each role can launch `claude`, `copilot`, `codex`, or `grok` via the `agent` field in profiles.
- **Observable Swarm** — Watch all agents in one window (tabs or panes on Windows, tmux panes on Unix). On WezTerm, a live color-coded status bar shows each `auto`-mode role's current state (waiting/receiving/delegating/handoff) regardless of which tab or pane is focused.
- **Cross-Platform** — Works on Windows, macOS, and Linux with zero duplication.

---

## Constitution and Roles

The recommended project layout is:

```text
kiln/
  project/
    roles/
      architect.md             # Architect role (design review, approval)
      coder.md                 # Coder role (TDD implementation)
      refactorer.md            # Refactorer role (quality gates, refactoring)
      specifier.md             # Specifier role (Gherkin acceptance tests)
      reviewer.md              # Reviewer role (batch review alternative to refactorer)
      selftest.md              # Selftest role (communication chain validation)
      human-in-the-loop.md    # Human-in-the-loop role (human-facing intake and approval checkpoint)
    constitution/
      workflow.md              # Handoff protocol, branch discipline, queue format
      engineering.md           # Language, tools, dependencies, practices
      project.md               # Project-specific architecture, tech stack, quality gates

# Optional: Override default profiles (framework uses kiln/framework/profiles.json)
kiln.profiles.json           # Project-specific profiles (optional, at root)
```

**Note:** Configuration profiles are inherited from the framework default (`kiln/framework/profiles.json`). Projects can optionally override by creating `kiln.profiles.json` at the project root if they need custom profile definitions.

### Profile Loading & Inheritance

Configuration profiles define which agents run, which roles they take, and where they work. Kiln uses a cascading search to find profiles:

1. **Project root** (`kiln.profiles.json`) — Project-level overrides
2. **Project config** (`kiln/profiles.json`) — Not used (projects don't copy profiles here; not to be confused with `kiln/project/`, the customizable constitution/roles/skills bucket)
3. **Project state** (`.kiln/profiles.json`) — Not used
4. **Framework** (`kiln/framework/profiles.json`) — Default profiles for all projects
5. **User home** (`~/.kiln/profiles.json`) — User-level defaults (optional)
6. **System** (`/etc/kiln/profiles.json`) — System-wide defaults (optional)

By default, **all projects use the framework's `kiln/framework/profiles.json`**, which defines the standard 4-agent workflow (specifier, coder, refactorer, architect). This means new projects work immediately without configuration.

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
  - **Codex agents**: `AGENTS.md` in the worktree root — Codex CLI's own project-instructions convention (confirmed against the installed binary's string table)
  - **Grok**: not yet supported — see Known Limitations

This ensures every agent operates with full constitutional context plus its specific role directives.

**Worker Subagent Assembly (`auto`-mode roles):** each of these agents is a thin, persistent **shell** — it only listens, merges, commits, and hands off. The actual role work is delegated each cycle to a disposable **worker**, built from the role file (`roles/<role>.md`) plus the `engineering.md` and `project.md` constitution — **not** `workflow.md`, since handoff/messaging protocol stays the shell's concern, not the worker's. The dispatch mechanism differs per backend:

- **Claude**: worker defined in a generated `.claude/agents/<role>-worker.md`, dispatched via Claude Code's `Agent` tool (blocking, deterministic — the shell explicitly invokes `subagent_type: "<role>-worker"`). No access to the `Agent` tool itself (no recursive subagent spawning) and no MCP messaging tools — it can only read/write/edit/test in its worktree. Its full working transcript never enters the shell's own context — only its final report does, which is what keeps the shell's context small and repetitive cycle over cycle, rather than filling up with the noise of the actual implementation work.
- **Copilot**: worker defined in a generated `.github/agents/<role>-worker.agent.md` (GitHub Copilot CLI's custom-agent format), dispatched by prose instruction — the shell's loop template tells it to delegate to the named custom agent, and Copilot CLI's own harness resolves that to a subagent call with its own isolated context window. `tools:` is scoped to `read, write, shell` — no MCP server names listed, so it has no messaging access, mirroring the Claude worker's isolation. Unlike Claude's `Agent` tool, this delegation is the model's own judgment call rather than a guaranteed deterministic invocation — GitHub has tuned Copilot CLI to be more selective about delegating on its own, so the wrapper prompt explicitly instructs it to always delegate even when it judges it could finish faster itself.
- **Codex**: worker defined in a generated `.codex/agents/<role>-worker.toml` (Codex CLI's own project-scoped custom-agent format — required fields `name`, `description`, `developer_instructions`; confirmed against official docs at `developers.openai.com/codex/subagents`), dispatched via Codex's built-in multi-agent spawn tools (`spawn_agent`/`assign_agent_task`/`wait_agent`/`close_agent` — the `multi_agent` feature, stable and enabled by default, confirmed directly against a live `codex.exe` install). `mcp_servers = {}` in the worker's TOML excludes messaging access, mirroring the Claude/Copilot worker's isolation.

### Default Workflow

The default four-agent workflow runs in a continuous loop. Each Claude shell agent's generated `CLAUDE.md` combines a role file with a **loop template** that drives the cycle through two skills — `/kiln-receive` and `/kiln-handoff` (`kiln/project/skills/kiln-receive`, `kiln/project/skills/kiln-handoff`) — plus a delegated dispatch to that role's worker subagent in between:

1. **`/kiln-receive`** — calls `wait_for_message()` via the `kiln-channel` MCP server (blocks until a handoff arrives), persists the message to `tmp/handoff-in.md` (survives auto-compact), merges the sender's commit (`git merge <commit>`), and logs a `[RECEIVED]` entry to `logbook.md`
2. **Delegate the work** — the shell does not implement anything itself. It invokes the `Agent` tool (`subagent_type: "<role>-worker"`, blocking) with the handoff content and current branch/worktree; the worker subagent does the actual role-specific task (see below) and reports back what it did. `specifier` still additionally requires explicit user approval before continuing — it runs in `manual` mode and is not yet part of this delegation pattern (see Known Limitations).
3. **Retry or escalate on failure** — if the worker reports it couldn't finish, the shell re-dispatches it once more with the failure as feedback; a second failure escalates to a handoff that reports the blocker instead of silently stalling.
4. **`/kiln-handoff`** — logs a `[SENT]` entry, squashes work commits into one, `INSERT`s the handoff into `.kiln/messages.db` via `write_query`, then reads it back to verify the row landed — retrying the INSERT if it didn't
5. **Immediately return to step 1, in the same turn** — a sent and verified handoff is not the end of the cycle; the loop template is explicit that the turn isn't over until `/kiln-receive` has run again (this closes a stall we found in live testing, where an agent would finish a verified handoff and simply stop instead of waiting for the next message)

**Copilot follows the same shape** (receive → delegate → retry-once-on-failure → handoff → loop again in the same turn) but via its own inline polling loop (`loop-auto-copilot.md`) rather than the `/kiln-receive`/`/kiln-handoff` skills — it polls `messages` directly via SQL (`read_query`/`write_query`), since Copilot has no blocking `kiln-channel` MCP tool, and squashes/logs the same way inline rather than through a shared skill file.

**Codex follows the same shape too** (`loop-auto-codex.md`) — poll via SQL (same as Copilot, no blocking channel), delegate to the `<role>-worker` custom agent via Codex's built-in multi-agent spawn tools (`spawn_agent`/`assign_agent_task`/`wait_agent`/`close_agent`), retry once on failure, then squash/log/handoff inline, same as Copilot. `manual` mode is also available for Codex (e.g. for a human-supervised role like `specifier`) using `loop-manual-codex.md`, same as any other backend.

The cycle flows: **specifier → coder → refactorer → architect → specifier**

- **`specifier`** — runs in **manual** mode: at startup, asks the user what feature to specify, writes Gherkin acceptance tests, and requires explicit user approval before sending the handoff to coder. All other roles run in **auto** mode (no human approval step in the loop).
- **`coder`** — Implements behavior slices using strict TDD until all tests pass, then sends handoff to refactorer.
- **`refactorer`** — Runs quality gates (coverage → CRAP → DRY → mutation site count), refactors for testability, sends handoff to architect.
- **`architect`** — Reviews module structure, runs pre-handoff verification (mutation → DRY → soft Gherkin), sends completion back to specifier.

> **Optional role:** `reviewer` is an alternative to `refactorer` with a focus on batch processing and review pipelines. Add it to your profile in `kiln/framework/profiles.json` to use it instead. See `kiln/project/roles/reviewer.md`.
>
> **Optional role:** `human-in-the-loop` is a human-facing intake and approval checkpoint ahead of the cycle, for profiles where `specifier` itself runs in `auto` mode with no user present. The framework's **`default` profile** (`kiln/framework/profiles.json`) pairs it with an autonomous specifier: `human-in-the-loop` (manual, `@current`) gathers and confirms a request with the user, hands it to `specifier` (now `auto`, its own worktree), which runs its normal Gherkin workflow non-interactively and forwards the eventual architect completion report back to `human-in-the-loop` for the user to see. See `kiln/project/roles/human-in-the-loop.md` and `kiln/project/roles/specifier.md` → "Auto-Mode Worker Entry Point".

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
   # Run the default 'default' profile (human-in-the-loop intake feeding an autonomous specifier -> coder -> refactorer -> architect cycle)
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
   - Generated `CLAUDE.md` (Claude agents) or `.github/copilot-instructions.md` (Copilot agents) in each worktree with embedded constitution + project + role content
   - Generated worker agent definitions for `auto`-mode roles — `.claude/agents/<role>-worker.md` (Claude) or `.github/agents/<role>-worker.agent.md` (Copilot) — the worker definition the shell delegates its actual work to each cycle
   - Per-worktree `.mcp.json` with both `kiln-db` and `kiln-channel` configured (correct role and branch env vars injected)
   - Channel log files at `.kiln/logs/channel-<role>.log` for debugging
   - Claude Code debug log files at `.kiln/logs/claude-debug-<role>.log` (`--debug-file`) for diagnosing stalls after the fact
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
   - Generates `.claude/agents/<role>-worker.md` for Claude agents (see Known Limitations — the receive/delegate/handoff loop that dispatches to this worker is currently Windows-validated only)

---

## Configuration Profiles

Kiln uses JSON profiles to define swarm topology. The default profile is `default`, whose name is set by the top-level `"default"` key in `kiln/framework/profiles.json` (`Get-KilnDefaultProfileName` in `lib/profile-loader.ps1` resolves it at launch if `-ProfileName` isn't given). All projects inherit the framework's default profiles from `kiln/framework/profiles.json` automatically.

**To customize profiles for a specific project**, create `kiln.profiles.json` at your project root. Kiln will use your custom profiles instead of the framework defaults.

### Framework Default Profile

The framework's `default` profile pairs a human-facing intake role with a fully autonomous specifier → coder → refactorer → architect cycle: `human-in-the-loop` runs `manual` in the main directory (`@current`) to gather and confirm the request with you, then the other four roles run `auto` in their own worktrees with no human input needed. Each `auto` role is a Haiku shell that delegates the actual work to a Sonnet worker subagent each cycle (see "Decoupling shell and worker models" below):

```json
{
  "profiles": {
    "default": {
      "description": "Human-guided request intake (human-in-the-loop) feeding a fully autonomous specifier -> coder -> refactorer -> architect cycle",
      "terminals": [
        {
          "role": "human-in-the-loop",
          "agent": "claude",
          "worktree": "@current",
          "mode": "manual",
          "model": "claude-sonnet-5"
        },
        {
          "role": "specifier",
          "agent": "claude",
          "worktree": "specifier",
          "mode": "auto",
          "model": "claude-haiku-4-5-20251001",
          "workerModel": "claude-sonnet-5"
        },
        {
          "role": "coder",
          "agent": "claude",
          "worktree": "coder",
          "mode": "auto",
          "model": "claude-haiku-4-5-20251001",
          "workerModel": "claude-sonnet-5"
        },
        {
          "role": "refactorer",
          "agent": "claude",
          "worktree": "refactorer",
          "mode": "auto",
          "model": "claude-haiku-4-5-20251001",
          "workerModel": "claude-sonnet-5"
        },
        {
          "role": "architect",
          "agent": "claude",
          "worktree": "architect",
          "mode": "auto",
          "model": "claude-haiku-4-5-20251001",
          "workerModel": "claude-sonnet-5"
        }
      ],
      "layout": {
        "tabs": [
          {
            "title": "Human-in-the-Loop",
            "panes": [{"role": "human-in-the-loop"}]
          },
          {
            "title": "Autonomous Cycle",
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
    }
  }
}
```

### Other Bundled Profiles

`kiln/framework/profiles.json` also ships:

- **`compact`** — the standard 4-agent swarm (specifier, coder, refactorer, architect; no `human-in-the-loop`), all in one tab as a 2×2 grid.
- **`tabs`** — the same 4-agent swarm, one role per tab instead of a grid.
- **`dual-pane`** — the same 4-agent swarm across two tabs, two roles side-by-side per tab.

Switch to any of these with `-ProfileName <name>` (Windows) or `--profile <name>` (Unix).

**Terminal fields:**

- **role** — maps to `kiln/project/roles/<role>.md` (must exist)
- **agent** — which AI tool to use: `claude`, `copilot`, `codex`, or `grok`
- **worktree** — `@current` to work in the main directory, or any name (creates `.worktrees/<name>/`)
  - Use `@current` for coordinator/review roles that work on the current branch
  - Use separate worktree names for roles that need isolation (e.g., each agent on its own branch)
- **model** — (Claude agents only) which Claude model to use, e.g., `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-4-8`
- **workerModel** — (Claude agents only, `mode: "auto"` roles only, optional) pins the `<role>-worker` subagent this shell dispatches each cycle to a different model than the shell itself. If omitted, the worker subagent inherits the shell's model (Claude Code's default behavior for subagents with no `model` frontmatter).

**Decoupling shell and worker models:** In Phase 6 (Shell + Worker-Subagent Delegation), the persistent wrapper shell only does `LISTEN → DELEGATE → SEND` — it never reasons about the actual task, that's entirely the worker subagent's job. This means the shell can run on a cheap/fast model (e.g. Haiku) while the worker that does the real TDD/implementation work runs on a stronger model (e.g. Sonnet):

```json
{
  "role": "coder",
  "agent": "claude",
  "worktree": "coder",
  "mode": "auto",
  "model": "claude-haiku-4-5-20251001",
  "workerModel": "claude-sonnet-5"
}
```

This is wired via Claude Code's subagent `model:` frontmatter field: `Write-GeneratedWorkerAgent` in `bin/kiln.ps1` writes `model: <workerModel>` into the generated `.claude/agents/<role>-worker.md` file when `workerModel` is set. Claude Code resolves a dispatched subagent's model from its own frontmatter, independent of the parent session's model — so the Haiku shell's worker subagent genuinely runs as Sonnet, not Haiku. The framework's `default` profile (`kiln/framework/profiles.json`) demonstrates this: `specifier`/`coder`/`refactorer`/`architect` shells run on Haiku, their workers run on Sonnet.

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

If no profile is specified, the framework's `default` profile is used. The working directory argument is required.

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

### Live Agent Status (WezTerm)

Each `auto`-mode role's wrapper cycles through four states — **waiting** (idle, blocked on the next message), **receiving** (a message just arrived; persisting/merging/logging it before delegating), **delegating** (the worker has been dispatched and is doing the actual work), and **handoff** (sending the result) — signaled at each transition by `python .kiln/tools/set-status.py <role> <state> [detail]`. This writes two things:

- **`.kiln/status/<role>.json`** — `{"role", "state", "detail", "since", "title"}`. Always reliable, readable on any platform/terminal.
- **A terminal title OSC sequence** — unreliable on its own: the agent CLI running in that same pane also writes its own title on every render tick (spinner frames, idle icon, ...) and, updating far more often, usually wins the race.

On WezTerm, Kiln's generated Lua config polls the status JSON files directly (not the contested pane title) roughly once a second and renders a live, color-coded status bar in the top-right of the window — one badge per role, background colored by state (green = waiting, blue = receiving, red = delegating, violet = handoff), visible regardless of which tab or pane is focused. This is what makes state visible even in grid/pane layouts like `compact`, where multiple roles share a single tab and would otherwise have no per-pane title of their own.

On Windows Terminal, there's no equivalent scripting hook for a composite status bar — you can still read the JSON files directly (e.g. `Get-Content .kiln/status/coder.json`) to see live state.

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
- Generated worker agent files (`.claude/agents/*-worker.md`, `.github/agents/*-worker.agent.md`) — hand-authored custom agents alongside them are preserved
- Root `.mcp.json` (generated for `@current`-mode roles)
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

Create a `selftest` profile in `kiln/framework/profiles.json` with the `selftest` role as the **first entry**:

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

After the test completes, `bin/kiln-db.ps1` (Windows) wraps the common queries so you don't have to hand-write SQL:

```powershell
.\bin\kiln-db.ps1 stats                     # message counts by status (queued/delivered/processed)
.\bin\kiln-db.ps1 list-messages selftest    # all messages for a role, optionally -Status <status>
.\bin\kiln-db.ps1 show-message <id>         # full content of one message
.\bin\kiln-db.ps1 retry-message <id>        # move a message back to 'queued' from delivered/processed
.\bin\kiln-db.ps1 clear-old -Before "-7 days"  # dry-run + delete old processed messages
```

Or query directly (any platform):

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
6. **Query database directly**: `.\bin\kiln-db.ps1 stats` (or `sqlite3 .kiln/messages.db "SELECT COUNT(*) FROM messages;"`) to verify messages were inserted
7. **Check agent permissions**: Ensure agents have MCP tool permissions in `.claude/settings.json`
8. **Check the agent's own reasoning**: `.kiln/logs/claude-debug-<role>.log` captures what the agent was actually doing/deciding, if it stalled without an obvious cause in the message queue or channel log

---

## Project Maturity & Status

**Kiln v0.2 — PHASE 6: SHELL + WORKER-SUBAGENT DELEGATION ✅ LIVE-VALIDATED**

### ✓ Completed Features

- **Phase 1: Framework Architecture** — Config-driven swarm orchestration, role injection, git worktree isolation
- **Phase 2: Cross-Platform Infrastructure** — Windows (PowerShell/Windows Terminal/WezTerm), Unix/macOS (zsh/tmux)
- **Phase 3: Auto-Agent Communication** — SQLite message queues with MCP server, automated role-based message forwarding, full agent chain test passing
- **Phase 4: Channel-Based Messaging** — Replaced SQL inbox polling with a blocking `wait_for_message()` Channel
  - ✓ `kiln-channel` Python MCP server (`kiln/framework/mcp-server/channel.py`) — polls SQLite and blocks until a message arrives, returns it already marked delivered
  - ✓ Per-worktree `.mcp.json` generated with `kiln-db` + `kiln-channel`, correct `KILN_ROLE`/`KILN_BRANCH` env vars injected per agent
  - ✓ Channel debug logs at `.kiln/logs/channel-<role>.log`
  - ✓ `-Stop` flag on `kiln.ps1` to kill orphaned MCP server processes after terminal close
- **Phase 5: Skill-Based Handoff Hardening** — Moved the raw receive/handoff mechanics out of the loop templates into two dedicated skills, and closed stall/merge failure modes found through live multi-cycle testing against the LibraryHub example
  - ✓ `/kiln-receive` and `/kiln-handoff` skills (`kiln/project/skills/kiln-receive`, `kiln/project/skills/kiln-handoff`) own the full receive/send sequence, including verify-and-retry on the handoff INSERT
  - ✓ Loop templates' "not end-of-turn" guardrail now explicitly covers looping back to `/kiln-receive`, not just the handoff-sent step — closes a confirmed stall where an agent finished a verified handoff and simply stopped instead of waiting for the next message
  - ✓ `.gitignore` fixes for symlinked/regenerated paths (`.kiln`, `CLAUDE.md`, `.mcp.json`, `tmp/`) that were getting accidentally committed and causing every `/kiln-receive` merge to hit conflicts
  - ✓ `.gitignore` is now committed before any worktree is created, even in a pre-existing repo, so new worktrees actually inherit it
  - ✓ Per-agent Claude Code debug logs (`--debug-file`) at `.kiln/logs/claude-debug-<role>.log`
  - ✓ `kiln-db.ps1` CLI (`list-messages`, `show-message`, `stats`, `retry-message`, `clear-old`) for inspecting the message queue without hand-writing SQL
- **Phase 6: Shell + Worker-Subagent Delegation** — Makes Claude, `auto`-mode role agents thin shells that delegate their actual work to a disposable worker subagent each cycle, keeping the shell's context small and repetitive instead of accumulating the full working transcript
  - ✓ `Write-GeneratedWorkerAgent` (`kiln.ps1`) generates `.claude/agents/<role>-worker.md` — role file + `engineering.md` + `project.md`, no `workflow.md`, no `Agent`/MCP tools
  - ✓ `loop-auto-claude.md` implements 7-step receive→mark→delegate→handle-failure→handoff→mark-processed→loop cycle with explicit message state transitions
  - ✓ **Live-validated** through 8+ cycles of LibraryHub multi-agent workflow with 50 tests, clean commits, zero stalls or message loss
- **Phase 6a: Message Lifecycle Tracking** — Full visibility into agent work and recovery from interruptions
  - ✓ `kiln-channel` MCP server adds `mark_processing()` and `mark_processed()` tools for state transitions
  - ✓ Message states: `queued` (created) → `delivered` (retrieved) → `processing` (work started) → `processed` (complete)
  - ✓ `wait_for_message()` checks for both `queued` and `delivered` messages, allowing recovery of unprocessed messages if an agent times out
  - ✓ Full state visibility via `kiln-db.ps1 stats` and database queries

### Current Capabilities

- ✓ Multi-agent swarms (2-5 agents typical)
- ✓ Per-role configuration and role injection
- ✓ Isolated git worktrees with branch naming (e.g., `feature/ABC-coder`, `main-refactorer`)
- ✓ Blocking Channel messaging with full lifecycle tracking — agents call `wait_for_message()` and transition messages through `queued` → `delivered` → `processing` → `processed` states
- ✓ Message recovery — if an agent times out after receiving, the next agent can pick up the message (still in `delivered` state)
- ✓ Skill-based receive/handoff sequence (`/kiln-receive`, `/kiln-handoff`) with verify-and-retry on the handoff INSERT
- ✓ Layered constitution system (workflow, engineering, project)
- ✓ Cross-platform terminal support (Windows Terminal, WezTerm, tmux)
- ✓ Flexible terminal layouts (tabs, split panes, grids, focus layouts)
- ✓ Per-agent model configuration for Claude agents
- ✓ Built-in communication health check (selftest agent)
- ✓ Logbook tracking of all handoffs and agent actions
- ✓ Shell + worker-subagent delegation for Claude `auto`-mode roles — persistent thin shells dispatch work to disposable worker subagents, keeping shell context at ~140 lines through unlimited cycles
- ✓ Codex agent support, including worker-subagent delegation via Codex's own multi-agent spawn tools — generated `AGENTS.md` + `.codex/agents/<role>-worker.toml`, isolated per-role `CODEX_HOME` MCP config, `--dangerously-bypass-approvals-and-sandbox` launch flag

### ⚠️ Security Considerations

**Agent Permissions:** Kiln agents run with **full permission rights by default** to enable seamless autonomous operation:

- **Claude agents**: `--permission-mode bypassPermissions` (auto-approve all MCP tools and file operations)
- **Copilot agents**: `--allow-all` (auto-approve GitHub Copilot tools and file access)
- **Codex agents**: `--dangerously-bypass-approvals-and-sandbox` (auto-approve all tool calls and disable the sandbox — Codex's own explicitly-named equivalent). Each Codex role also gets its own isolated config directory via the `CODEX_HOME` env var (`.kiln/codex-home/<role>/`), so Kiln never overwrites your real `~/.codex/config.toml`.
- **Grok**: not implemented — see Known Limitations

This means agents can read/write/execute any file in their worktree without prompting. This is intentional for autonomous development workflows but should be understood as a security trade-off.

**Risk mitigation:**

- Keep Kiln projects in isolated, non-production directories
- Do not run agents with sensitive data (credentials, secrets, PII) in the project
- Use git worktrees for isolation — agents can only access their assigned worktree and shared `.kiln/` directory
- Review agent outputs and commits before merging to main branch
- Consider running Kiln in a sandbox/VM for untrusted code or high-security scenarios

### Known Limitations & Future Work

- **Real feature workflows are continuously validated** — multi-cycle specifier → coder → refactorer → architect chains run successfully against the LibraryHub example; 8+ cycle test runs show stable state flow with 34+ processed messages and zero stalls or message loss
- **Error handling** — Minimal error recovery in agent workflows; graceful degradation not yet implemented
- **Scaling** — Tested with 4 agents over 8+ cycles with stable performance; behavior with 10+ agents unknown
- **Multi-agent backend validation** — Framework supports `claude` (validated, including Phase 6 shell+worker delegation live-tested through 8+ cycles), `copilot` (worker delegation implemented and confirmed against a live CLI session, but not yet exercised through a full multi-cycle swarm run the way Claude has been), and `codex` (worker delegation via Codex's own multi-agent spawn tools — MCP config, `AGENTS.md`/worker-`.toml` generation, and TOML validity verified directly against a live `codex.exe` install and official docs, but not yet exercised through a full multi-cycle swarm run or live spawn_agent call, since that requires `codex login` first). `grok` is not implemented: the actual installed `grok` CLI in this environment turned out to be a third-party project (`grok-cli-hurry-mode`, not an official xAI tool) whose persistent/interactive session has no non-interactive auto-approve path in its current build (confirmed by reading its bundled source) — only its one-shot `-p` headless mode auto-approves, which can't sustain Kiln's persistent per-role session model without a fundamentally different poll-and-relaunch design.
- **`kiln.sh` has no loop/runtime template injection for Claude/Copilot** — Unix agents are launched from a much thinner instruction file than Windows' generated `CLAUDE.md`, with no `auto`/`manual` mode concept; the receive→delegate→handoff loop and Phase 6's delegation pattern may not be active there until this pre-existing gap is closed. (Codex's `kiln.sh` path was built to full parity with `kiln.ps1` regardless — its wrapper prompt and `.codex/agents/<role>-worker.toml` are hand-assembled rather than routed through the template mechanism, since `kiln.sh` has no `Get-KilnTemplate`-style loader at all, but the content and delegation pattern match.)

### Recommended Next Steps

1. **Run real feature workflows** — Test specifier → coder → refactorer → architect chain with actual code implementation
2. **Add error handling** — Implement graceful failure modes when agents can't process messages
3. **Multi-language projects** — Test with Python, Kotlin, JavaScript projects beyond the LibraryHub FastAPI example
4. **CI/CD integration** — Determine how to integrate Kiln agents into GitHub Actions / GitLab CI workflows
5. **Validate Phase 6 against `selftest`, then LibraryHub** — confirm worker-subagent dispatch and Skill invocation work as designed before rolling the pattern out further
6. **Bring `kiln.sh` up to parity** — port the loop/runtime template injection Windows already has, so Phase 6 (and future loop changes) apply equally on Unix

---

## Acknowledgments

Kiln was inspired by [Uncle Bob's swarm-forge](https://github.com/unclebob/swarm-forge), a framework for multi-agent development. While taking cues from that design philosophy, Kiln evolves the concept with a focus on TDD-driven workflows, MCP messaging standards, and production-ready orchestration for AI agents across multiple languages and platforms.


