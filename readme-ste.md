<p align="center">
  <img src="docs/images/logo.png" alt="Kiln logo" width="120" />
</p>

# Kiln — Simplified Technical English Edition

> **About this document.** This is [README.md](README.md) written in ASD-STE100
> Simplified Technical English. The content is the same. Only the language is different.
>
> The rules of the standard control this document:
> one meaning for each word, one part of speech for each word, the active voice,
> simple verb tenses, a maximum of 20 words in an instruction, a maximum of 25 words in a
> description, and a maximum of 6 sentences in a descriptive paragraph.
> Technical Names and Technical Verbs stay, because the standard permits them.
> The list of Technical Names for this document is at the end.
>
> Code, file names, command lines, tables and example output do not change.
> The standard applies to the sentences, not to the data.

**Kiln is an orchestration platform. It makes a group of AI agents into reliable software
engineers.**

Kiln starts a multi-agent swarm from a configuration file. Each agent operates in its own git
worktree. Each agent has instructions for its role. The agents send messages to each other.

Kiln knows about git. Kiln makes a sub-branch for each worktree. Kiln keeps all state in
`.kiln/`. Kiln records each handoff in `logbook.md`.

**Kiln is a Python application.** The files `bin/kiln.ps1` and `bin/kiln.sh` are thin shims.
Each shim puts `kiln/framework/` on `PYTHONPATH`. Then the shim starts `python -m launcher.cli`.
All platforms run the same code. Only the terminal backends are different. The terminal
backends are WezTerm, Windows Terminal and tmux.

---

## Quick Start

To get a Kiln project quickly, run the install script.

### 1. Make a New Project

**Windows (PowerShell):**
```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\path\to\my-project
cd C:\path\to\my-project
```

**Unix/macOS:**
```bash
./bin/kiln.sh init /path/to/my-project
cd /path/to/my-project
```

This command makes a complete Kiln project. The project contains the configuration, the role
files and a git repository.

**Note:** Two scripts, `kiln-init.ps1` and `kiln-init.sh`, did this before. The main entry
point does it now. The two scripts are no longer in the repository.

### 2. Use an Example (Optional)

You can include an example project brief. Add the `-Example` flag. Give the name of a directory
in `examples/`. The names are `library-hub`, `library-hub-java` and `battlezone`.

**Windows:**
```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\path\to\library-hub -Example library-hub
```

**Unix/macOS:**
```bash
./bin/kiln.sh init /path/to/library-hub --example library-hub
```

This command copies `examples/<name>/README.md`. The file becomes the project brief. The
command also copies the files in `examples/<name>/kiln/project/constitution/`. These files
replace the default files. A Java example supplies its own `project.md` and `engineering.md`.
For the full list, refer to **Examples**.

### 3. Start the Swarm

**Windows:**
```powershell
.\bin\kiln.ps1 -WorkingDir .
```

**Unix/macOS:**
```bash
./bin/kiln.sh .
```

The procedure is complete. Kiln makes the git worktrees. Kiln writes the role files. Kiln
starts the agents in your terminal.

For more options, refer to **Running Kiln**.

---

## What Kiln Does

Kiln is a thin orchestration layer. Kiln does the tasks that follow.

- Kiln starts a **swarm from a configuration file**. For each agent, you specify the role, the
  AI backend and the workspace. The four backends are `claude`, `copilot`, `codex` and `grok`.
  All four backends operate in scheduler mode. The `grok` backend has no wrapper mode. Refer to
  **Known Limitations**.
  - Kiln reads the default configuration from `kiln/framework/profiles.json`.
  - A project can replace the defaults. Make a file `kiln.profiles.json` in the project root.
  - The terminal layouts are flexible. You can use tabs, split panes, grids or a tree.
- Kiln makes **one terminal tab or window for each role**. You can look at all agents at the
  same time.
  - WezTerm operates on all platforms and gives all the functions. It shows the live status
    badges in the tab bar. It makes the split-pane layouts, the grid layouts and the inbox
    pane.
  - Windows Terminal and tmux are the alternatives. They start the same swarm. They do not
    show a live status badge. They do not make split-pane layouts or grid layouts.
- Kiln reads the behavior of a role from the file `kiln/project/roles/<role>.md`. Kiln also
  reads the constitution in `kiln/project/constitution/`. The constitution has three layers:
  workflow, engineering and project.
- Kiln makes **one git worktree for each agent** in `.worktrees/`. Thus the agents do not
  interfere with each other. An agent with the value `@current` is different. That agent
  operates in the project root, on the current branch.
- Kiln lets each role use a different **agent backend**. Set the `agent` field in the profile.
  The permitted values are `claude`, `copilot`, `codex` and `grok`.
- Kiln makes a **message system between the agents**. The system uses SQLite in the file
  `.kiln/messages.db`. It records the full lifecycle of each message. Two MCP servers give
  access to it:
  - **`kiln-db`** reads and writes SQL. An agent uses the `query` tool to send a handoff.
  - **`kiln-channel`** supplies the `wait_for_message()` tool. The tool blocks. Each agent
    calls it to receive the next handoff. The channel examines the database. When a message
    comes, the channel returns it with the status `delivered`. The channel also supplies
    `mark_processing()` and `mark_processed()`. These two tools change the status of a message.
  - **The lifecycle of a message** has four steps. First `queued`, when the sender makes it.
    Then `delivered`, when the agent receives it. Then `processing`, when the work starts. Then
    `processed`, when the agent sends its handoff.
- Kiln keeps all swarm state in `.kiln/`. This directory holds the logs, the sessions and the
  message database. Git ignores this directory. The content is temporary.

![Kiln running the default profile: a Human-in-the-Loop tab alongside an Autonomous Cycle tab showing specifier, coder, refactorer, and architect in a 2×2 grid, each with a live status badge](docs/images/kiln1.png)

*The default profile in WezTerm. One tab has the role for the human. The other tab has the four
autonomous roles in a grid.*

### The Project Structure that `bin/kiln.ps1 -Init` and `bin/kiln.sh init` Make

When you run the init command, it makes this structure:

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
│   ├── traffic.db               # Captured API traffic — only with --proxy, see "Traffic Capture"
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

**Important points:**
- Git records the directory `kiln/project/`. It holds the constitution, the roles and the
  skills. This is the copy for your project. You can change it as necessary.
- The directories `.kiln/` and `.worktrees/` are temporary. Git ignores them.
- A project gets its profiles from the framework. To replace them, make the file
  `kiln.profiles.json` in the project root.

---

## Platform Support

One Python implementation operates on all platforms. The shell scripts are shims. Each shim
finds the framework, sets `PYTHONPATH`, and sends its arguments to `python -m launcher.cli`.
There is only one implementation. Thus a second implementation cannot become obsolete.

**Requirements for all platforms:**

- Python 3.11 or a later version. The launcher, the scheduler and the MCP servers are Python
  programs.
- Git.
- One agent CLI or more. The applicable CLIs are Claude Code, GitHub Copilot and Codex. Your
  configuration controls which CLIs you must have.
- The MCP servers `kiln-db` and `kiln-channel`, if you use wrapper-mode roles. To install them,
  run `pip install -r kiln/framework/mcp-server/requirements.txt`. Scheduler-mode roles do not
  need an MCP server.

**Note for Debian and Ubuntu:** That command stops with the error
`error: externally-managed-environment`. This is the result of PEP 668. Use this command:
`python3 -m pip install --user --break-system-packages -r ...`

**Caution:** Do not use a virtualenv for the MCP servers. The agent CLI starts the channel
server with an interpreter name from its own PATH. Thus that interpreter must be able to import
the SDK. If the import check fails, Kiln shows the correct command for your platform.

### Windows

```powershell
.\bin\kiln.ps1 -WorkingDir "C:\path\to\project"
```

- PowerShell 7 or a later version. Windows 11 includes it. The shim needs it.
- WezTerm or Windows Terminal. WezTerm gives all the functions. Windows Terminal is the
  alternative. To find what Windows Terminal does not do, refer to **Terminal Behavior**.

### Unix/Linux/macOS

```sh
./bin/kiln.sh /path/to/project
```

- Any POSIX shell. The first line of the shim is `#!/usr/bin/env bash`. You no longer need zsh.
- WezTerm or tmux. WezTerm gives all the functions and operates on all platforms. Thus Linux
  and macOS get the same tab-bar badges and the same layouts as Windows. tmux is the
  alternative. It makes one detached session for each role. It does not make split-pane layouts
  or grid layouts. It does not show a live status badge.

**Terminal selection on all platforms:** Use the flag
`--terminal wezterm | wt | tmux | none`. As an alternative, set the variable `KILN_TERMINAL`.

If you do not set the terminal, Kiln finds one. Kiln selects WezTerm when `wezterm` is on
`PATH`. If not, Kiln selects Windows Terminal on Windows, or tmux on Unix, Linux and macOS.
Refer to **Terminal Behavior**. The value `none` shows the commands but starts nothing. Use
`none` together with `--dry-run`.

---

## Framework Structure

This is the structure of the Kiln repository:

```text
kiln/
├── bin/                          # Entry points
│   ├── kiln.ps1                  # Windows shim -> python -m launcher.cli
│   ├── kiln.sh                   # Unix shim -> python -m launcher.cli
│   ├── kiln-cleanup.ps1          # Full project reset (Windows only — see Cleanup)
│   ├── clear-messages.ps1 / .sh  # Empty the message queue (testing utility)
│   └── kiln-db.ps1               # Inspect/manage messages.db (Windows only)
│
├── kiln/
│   ├── project/                  # Copied into every new project's kiln/project/ — customize freely
│   │   ├── constitution.md
│   │   ├── constitution/         # Shared constitution rules
│   │   │   ├── workflow.md           # Handoff protocol + the Handoff Routing table
│   │   │   ├── engineering.md        # Engineering practices & quality standards
│   │   │   └── project.md            # Project rules starter template
│   │   ├── roles/                    # Role prompts
│   │   └── skills/                   # Agent skills (optional)
│   │
│   └── framework/                # Never copied — read directly from this install
│       ├── launcher/                 # The launcher. Everything bin/ used to do.
│       │   ├── cli.py                    # Argument parsing and the launch sequence
│       │   ├── config.py                 # Profile loading, inheritance, validation
│       │   ├── paths.py                  # Every path Kiln touches, in one place
│       │   ├── commands.py               # The command injected into each pane
│       │   ├── generate.py               # CLAUDE.md / AGENTS.md / .mcp.json / worker files
│       │   ├── workspace.py              # gitignore, hooks, worktrees, skills
│       │   ├── scaffold.py               # `init` project scaffolding
│       │   ├── stop.py                   # `--stop` process teardown
│       │   └── terminals/                # One module per backend
│       │       ├── wezterm.py                # Generates the Lua config; WezTerm builds the panes
│       │       ├── windows_terminal.py       # Builds `wt.exe` argument lists
│       │       └── tmux.py                   # new-session / send-keys
│       │
│       ├── scheduler/                # The deterministic role loop (see "Scheduler Mode")
│       │   ├── role_scheduler.py         # One cycle = run_once(); main() is the only loop
│       │   ├── db.py                     # Every message-queue SQL statement
│       │   ├── routing.py                # Parses the Handoff Routing table
│       │   ├── handoff.py                # Handoff message format
│       │   ├── status_contract.py        # The KILN-STATUS: done|blocked contract
│       │   ├── worker_prompt.py          # Builds the one-shot worker invocation
│       │   ├── git_ops.py                # Merge, squash-to-anchor, local excludes
│       │   ├── pane_status.py            # The pinned status bar in each pane
│       │   ├── inbox.py                  # The human's notification pane (`kiln inbox`)
│       │   ├── dashboard.py              # Swarm-wide live view (`"scheduler": "dashboard"`)
│       │   ├── send.py                   # Queue a handoff from the CLI (`kiln send`)
│       │   └── adapters/                 # One module per backend: claude, codex, copilot, grok
│       │
│       ├── proxy/                    # Opt-in traffic capture (`--proxy`, see "Traffic Capture")
│       │   ├── server.py                 # Forwarding proxy; streams through, never buffers
│       │   └── capture.py                # Redaction, composition split, traffic.db schema
│       │
│       ├── profiles.json             # Default configuration profiles
│       ├── templates/                # Loop/runtime templates for wrapper-mode roles
│       ├── mcp-server/               # kiln-channel: blocking wait_for_message() receiver
│       └── tools/                    # set-status.py — re-seeded into .kiln/tools/ every launch
│
├── examples/                     # Example project briefs
├── tests/                        # pytest suite for launcher/ and scheduler/
└── docs/                         # Documentation & assets
```

**The directory `bin/`** holds the entry points. The two `kiln` scripts are shims and contain no
logic. The other files are independent utilities. They were never part of the launcher.

**The directories `kiln/framework/launcher/` and `kiln/framework/scheduler/`** hold the
implementation.

**The directory `kiln/project/`** goes into each new project. You can change your copy as
necessary.

**The directory `kiln/framework/`** does not go into a project. Kiln reads it from this
installation at start. Thus a change here has an effect on all projects that use this
installation.

> **Note:** Before the port to Python, the implementation was in `lib/`. It had a PowerShell
> tree and a shell tree in parallel: `profile-loader.{ps1,sh}`, `terminal-adapter.sh` and
> `terminal-adapters/*`. These files are deleted. To compare the behavior, look in the git
> history.

---

## Core Features

- **The topology comes from a configuration file.** The shape of the swarm comes from
  `kiln/framework/profiles.json`. It is not in the code.
- **The terminal layouts are flexible.** In your profile, you make the arrangement of the tabs
  and the panes. You can use simple tabs, split panes, 2x2 grids, trees or focus layouts. A
  focus layout has one full tab and a split of three panes below it.
- **Kiln puts the role into the agent.** Kiln merges the constitution and the role instructions
  into one instruction file. The constitution files are `workflow.md`, `engineering.md` and
  `project.md`. The role file is `roles/<role>.md`. The instruction file is `CLAUDE.md` or
  `.github/copilot-instructions.md`. Thus each agent has the full context immediately.
- **The project has its own constitution.** In `kiln/project/constitution/project.md`, you
  specify the architecture, the technical stack and the quality gates.
- **The rules are in layers.** The directory `kiln/project/constitution/` holds three files.
  The file `workflow.md` has the handoffs. The file `engineering.md` has the tools and the
  practices. The file `project.md` has the architecture and the quality rules. All three apply
  to each agent.
- **Each role selects its backend.** A role can start `claude`, `copilot`, `codex` or `grok`.
  Set the `agent` field in the profile.
- **A role has two possible execution modes.** In **wrapper mode**, an LLM obeys instructions
  in prose and controls the cycle. In **scheduler mode**, a Python state machine controls the
  cycle. Refer to the next section.
- **You can look at the swarm.** All agents are in one window. Each scheduler pane has a status
  bar with colors at the bottom. In WezTerm, the tab bar also shows a live badge for each role.
  The badge stays visible when a different tab or pane has the focus.
- **You can measure the token quantity.** The dashboard shows the tokens and the cache data for
  each role. The optional capture proxy (`--proxy`) records what each role sends. It divides
  each request into the tools, the instructions and the conversation.
- **Kiln operates on all platforms.** One Python implementation operates on Windows, macOS and
  Linux. Only the terminal backends are different.

---

## Execution Modes: Wrapper Mode and Scheduler Mode

Each role in `auto` mode does a cycle with five steps: receive, merge, work, squash and
hand off. Kiln can control this cycle in two ways. Set the `scheduler` field in the profile for
each role.

All four backends (`claude`, `copilot`, `codex` and `grok`) have a scheduler adapter. Thus all
`auto` roles in the supplied profiles use scheduler mode.

Use wrapper mode in two conditions only:

1. The role is the `manual` entry point for the human. That role must have a true conversation.
   The one-shot model of the scheduler cannot do this.
2. The backend has no scheduler adapter. **Note:** the wrapper mode for `grok` is not
   available. Refer to **Known Limitations**.

### Scheduler Mode (`"scheduler": "python"`, the Default for `auto` Roles)

A Python process controls the pane. The process examines SQLite directly. It merges the branch.
For each handoff, it starts the agent CLI **one time** as a subprocess. It reads the result. It
squashes the commits. It writes the next handoff. Then it does the cycle again.

The LLM only does the work. The LLM makes no decision about the control flow.

```jsonc
{ "role": "coder", "agent": "claude", "worktree": "coder",
  "mode": "auto", "model": "claude-sonnet-5",
  "scheduler": "python" }        // <- what every shipped `auto` role sets
```

### Wrapper Mode (for Manual Roles and Backends Without an Adapter)

A continuous LLM session stays in the pane. The session obeys the loop in the file
`kiln/framework/templates/loop-auto-<agent>.md`. The session gets access to the message queue
through two MCP servers. The server `kiln-db` does the SQL. The server `kiln-channel` supplies
the `wait_for_message()` tool, which blocks. The session decides when a turn is complete. The
session sends the work to a temporary worker subagent.

**Caution:** These mechanics are prose. Thus the model can read them incorrectly. A turn can
stop too soon. The model can omit a merge step. Scheduler mode removes this type of failure.
Use wrapper mode only when the role must have a live conversation.

The table shows the differences:

| | Wrapper mode | Scheduler mode |
| --- | --- | --- |
| Pane runs | a persistent LLM session | `python -m scheduler.role_scheduler` |
| Loop control | prose in a loop template | `role_scheduler.run_once()` |
| Queue access | `kiln-db` + `kiln-channel` MCP | direct SQLite |
| Turn is done when | the model decides | the worker prints `KILN-STATUS: done` |
| Routing target | model reads the routing table | `routing.py` resolves it |
| Retry / escalation | model judgement | one retry, then escalate to `human-in-the-loop` |
| MCP servers needed | yes | **no** |
| Generated `CLAUDE.md` | yes | **no** — deleted if a role switches over |

A sentinel is the contract between the two modes. The last line of a worker must be one of
these two lines:

```text
KILN-STATUS: done <one-line summary>
KILN-STATUS: blocked <what stopped it>
```

If the line is absent or incorrect, Kiln uses the status `blocked`. Thus a worker that is not
sure escalates. It does not report success incorrectly.

**The advantages and the disadvantages.** The scheduler cannot become tired. It cannot omit a
merge. It cannot forget to hand off. You can test the full cycle without an LLM.

But each invocation is one-shot. At each handoff, the worker starts again. It has no memory of
the last handoff. Thus all necessary data must be in the handoff, in the repository or in the
constitution.

All four adapters are one-shot subprocess invocations. The adapters are in
`kiln/framework/scheduler/adapters/{claude,copilot,codex,grok}_adapter.py`. Each adapter was
tested live against the non-interactive flags of its CLI.

The backends `claude` and `grok` report a cost in dollars. The backends `copilot` and `codex`
report only the tokens and the request counts. But each adapter reads the token usage from the
stream that it already reads. Thus a role that reports no dollars still reports tokens. The
dashboard then shows that the total cost is partial. A role that is absent from the total must
not look like an inexpensive role.

Each CLI needed one accommodation. All the accommodations are in the adapter, not in the
scheduler:

- The `copilot` CLI reads the MCP configuration from `~/.copilot/mcp-config.json`. It has no
  flag for each call. Thus the adapter disables the global `kiln-db` server at each invocation.
- The command `codex exec` has no flag to select an agent by name. Thus the adapter puts the
  persona of the worker directly into the prompt. Also, an isolated `CODEX_HOME` for each role
  has no `auth.json` and gives a 401 error. Thus the adapter uses the ambient `CODEX_HOME`,
  which has the authentication.
- The `grok` flag `--output-format streaming-messages-json` gives the same format as the
  Anthropic Messages API. Thus the `grok` adapter is almost the same as the `claude` adapter.

### Inbox Mode (`"scheduler": "inbox"`)

The inbox is a third type of pane. It is the part for the human. It runs `scheduler.inbox`. It
looks at the queue of a different role. It shows each message that comes. It sets the message to
`processed`. Then it rings the terminal bell. It has no agent, no worktree, no instructions and
no MCP.

```jsonc
{ "role": "inbox", "worktree": "@current", "title": "Kiln Inbox",
  "mode": "manual", "scheduler": "inbox",
  "watches": "human-in-the-loop" }    // whose queue to show
```

The inbox is necessary because one LLM session cannot listen and also be an entry point. The
wrapper role `human-in-the-loop` blocks in `wait_for_message()`. That tool examines the queue in
a `while True:` loop with **no timeout**.

Thus a session that listens cannot receive your input. And a session that speaks to you does not
listen. Escalations were in neither of the two conditions. The escalations stayed in the queue.

To correct this, Kiln divides the two functions. The inbox listens. Your Claude session speaks.

The `send` command is the other half. It writes a handoff directly. It uses no MCP and no LLM.
Thus you can start a cycle, or release a cycle, when the agents are the problem:

```powershell
.\bin\kiln.ps1 send --to specifier "Add CAT-4: search the catalog by author."
.\bin\kiln.ps1 inbox        # or run the watcher yourself, outside a swarm
```

Both commands find the queue and the current branch from the project.

**Caution:** The branch is important. Each message applies to one branch. An inbox on the
incorrect branch looks the same as an empty inbox.

### Dashboard Mode (`"scheduler": "dashboard"`)

The dashboard is a fourth type of pane. The inbox shows one role. The dashboard shows the full
swarm. It runs `scheduler.dashboard`. It is a live view, like the `top` command. It has no
agent, no worktree, no instructions and no MCP. In this, it is the same as the inbox.

```jsonc
{ "role": "dashboard", "worktree": "@current", "title": "Kiln Dashboard",
  "mode": "manual", "scheduler": "dashboard" }
```

The dashboard reads the data at an interval of 2 seconds. To change the interval, use
`--poll-interval`. At each interval, it erases the pane and writes a full frame.

The inbox and the pane status bar keep the scrollback. The dashboard does not, because the old
frames have no value.

```text
📊 Kiln Dashboard — library-hub-testrun (main)                     13:36:57
────────────────────────────────────────────────────────────────────────────────────────
ROLE                 STATE            SINCE      QUEUE  CYCLES     COST    TOKENS  CACHE
────────────────────────────────────────────────────────────────────────────────────────
human-in-the-loop    ● waiting       1h ago          0       -        -         -      -
inbox                ● -             -               0       -        -         -      -
specifier            ● waiting       1s ago          0       2    $0.35 238.4k tok    84%
coder                ● waiting       1s ago          0       1    $2.29  4.4M tok    97%
refactorer           ● waiting       1s ago          0       1    $2.10  4.5M tok    98%
architect            ● waiting       1s ago          0       1    $0.67  1.2M tok    97%
dashboard            ● -             -               0       -        -         -      -
────────────────────────────────────────────────────────────────────────────────────────
TOTAL COST: $5.41        TOTAL CYCLES: 5        TOKENS: 10.3M tok        ESCALATIONS: 0
  tokens by kind: in 412 · out 71.1k · cache-read 10.0M · cache-write 219.2k

Recent activity
  17:41:58  coder → refactorer            [Coder] Implement CAT-3 endpoint
  17:40:12  specifier → coder             [Specifier] Wrote acceptance criteria
  17:38:44  human-in-the-loop → specifier Approved request for CAT-3

Escalations
  (none in the recent window)
```

The state of each role comes from two sources. The file `.kiln/sessions` gives the list of the
roles. The file `.kiln/status/<role>.json` gives the state of one role. The queue depth and the
recent activity come from `messages.db`.

The dashboard shows the cost and the cycles only for the roles that report them. Scheduler roles
report them. To find where the numbers come from, refer to **Pane Status Bar**. Wrapper roles do
not record the cost or the cycles. Thus their cells show `-`. A value of `$0.00` would be
incorrect.

**Read the `CACHE` column first.** The tokens tell you that a role is expensive. The cache hit
rate tells you the cause. A high rate shows true work. A low rate shows a prompt that Kiln sends
again, uncached, at each cycle.

In the example, the four scheduler roles sent 10.3M input tokens to the model. Only
approximately 0.3M tokens had a charge as new tokens.

Examine a role that has a much lower rate than the other roles. In an earlier run, the cost for
each token was 2.7 times more for one role than for another. The full difference was the cache
behavior. The quantity of tokens was not the cause.

The `TOTAL COST` field shows a plus sign (`$5.41+`) when a role uses a backend that reports
tokens but no dollars. The backends `codex` and `copilot` do this. Thus a total that omits a
role has a label. Kiln does not report a value that is too low.

You can run the dashboard alone against any project. Use `python -m scheduler.dashboard --once
...`. For the necessary paths, refer to `--help`. As an alternative, start a profile that
includes the dashboard.

**To try it,** use the supplied `default` profile. It runs the four `auto` roles on the
scheduler. It keeps `human-in-the-loop` as an interactive session. It puts an inbox below that
role in the same tab. It gives the dashboard its own tab.

```powershell
.\bin\kiln.ps1 -WorkingDir .
```

Each scheduler pane starts with a configuration banner. The banner shows the role, the branch,
the worker, the model, the routing, the worktree, the queue, the timeouts and the path of the
log. Then the pane shows each cycle.

Kiln writes a log for each role to `.kiln/logs/scheduler-<role>.log`. Thus a scheduler that
stops with an error leaves data after its pane closes.

---

## Traffic Capture (`--proxy`)

The token counts tell you *that* a role is expensive. Only the body of the request tells you
*why*. The body shows the quantity of tool schemas, the quantity of instructions and the
quantity of conversation that Kiln sends again. To get this data, Kiln can send the API traffic
through a local capture proxy.

**The proxy is off. The flag `--proxy` starts it.** When the proxy operates, it records only the
metadata. The metadata is the sizes, the times, the model names and the token counts. This is
approximately 2.9 KB for each request. **The proxy does not record the text of the prompt.** For
that, you must give a second flag.

```powershell
.\bin\kiln.ps1 -WorkingDir .                         # no proxy, no capture store
.\bin\kiln.ps1 -WorkingDir . --proxy                 # metadata capture
.\bin\kiln.ps1 -WorkingDir . --proxy --capture full  # + request/response bodies
```

If you do not use the proxy, you keep the `COST`, `TOKENS` and `CACHE` columns. Each adapter
reads these values from its own CLI stream. The proxy does not supply them. Only the
prompt-weight panel needs the proxy.

**Caution:** The proxy has a cost. Each routed request goes through a local Python process. If
that process stops, the routed roles lose their access to the API. You must then start the swarm
again. No other process monitors the proxy.

### How the Proxy Routes the Traffic

The proxy replaces the base URL. **It is not a MITM proxy.** It needs no certificates. It does
no TLS interception. It makes no change to the trust store of the system.

Kiln points each role at `http://127.0.0.1:8787/kiln/<role>`. Thus the prefix of the path gives
the role for each captured request. Kiln puts this path into the environment with the existing
`AgentCommand.env` mechanism. Thus a one-shot worker gets the path from its pane.

Kiln routes the `claude` roles and the `codex` roles. **A live test shows that both operate
correctly.** Each CLI obeys the override. Each CLI also sends its own subscription credential to
the local host. Thus you need no API key.

The two CLIs get the value in different ways. Claude reads the variable `ANTHROPIC_BASE_URL`.
Codex has no such variable. Codex gets `-c model_providers.…` overrides on its command line.

Kiln does not route the `grok` roles or the `copilot` roles. These roles operate, but they are
not in the capture. The launcher writes a log of the roles that it routes. Thus an empty panel
always has an explanation.

One proxy is sufficient for both vendors. The prefix of the path gives the *role*, not the
backend. Thus each role that is not an Anthropic role gets a route. The route has the format
`--route <role>=<host>/<base-path>`. It tells the proxy where the traffic of that role must go.
The launcher makes these routes from the profile.

The default port is 8787. If that port is in use, **the proxy tries the next port**. Thus two
Kiln projects can operate at the same time.

Then the launcher waits. The proxy must accept a connection. If the proxy does not accept a
connection, the launcher stops the start procedure.

**Caution:** This test is necessary. A swarm can point at a port where the proxy of a *different*
project operates. That proxy sends the traffic correctly, but it records the traffic in the store
of the other project. All parts look correct.

The flag `--proxy-port` selects one port. If that port is in use, the launcher stops. It does not
try a different port.

The command `python -m proxy.server --stub` gives local answers. It sends nothing to the vendor.
Thus you can test the full configuration and use no tokens.

### What the Proxy Stores

The proxy writes to `.kiln/traffic.db`. It does not write to `messages.db`. This is deliberate.
The file `messages.db` holds the live state of the swarm, and persons open it in a SQLite
browser. The bodies of the requests are much larger. They would make that file difficult to use.

| Mode | Recorded |
|---|---|
| `metadata` (default) | timing, status, byte sizes, model, token usage, and the tools/system/messages split |
| `full` | the above plus request and response bodies, capped at 256 KiB each |

The bodies are the only part that becomes larger. A measurement of a true store shows this. The
store had 676 requests and a size of 107.6 MB. The bodies were **98.3%** of that size.

Thus `full` mode has a budget. When the bodies use more than 256 MB, the proxy erases the bodies
of the oldest rows. The rows stay. The proxy calculates the composition and the token counts at
the time of the capture. Thus a row without its body still supplies data to each panel. Only the
text of the prompt goes.

Metadata mode never gets to the budget, because it writes no bodies.

The proxy reads the formats of both vendors into the same columns. Anthropic sends `tools`,
`system` and `messages` as keys at the top level. The Responses API of Codex has none of these
keys. It puts all the data into one flat `input` array. Thus the proxy divides that array by the
type of each item.

The token usage needs the opposite care. Anthropic reports `input_tokens` as the *new* tokens
only. It counts the cache reads separately. OpenAI reports `input_tokens` as the total, and the
total *includes* the cache reads. Thus the proxy subtracts the cached quantity.

**Caution:** If you store one of these numbers with the meaning of the other, the cache hit rate
becomes approximately one half of the true value.

The proxy **never writes the values** of the `Authorization` header or an API-key header. It
keeps the names of the headers. Thus you can see what the CLI sent. It replaces the values.

**Warning:** In `full` mode, the bodies contain the full source code that the agent read, as
plain text. Give this store the same protection as the repository.

The composition data is available in `metadata` mode. The proxy calculates the sizes at the time
of the capture. Thus you can measure the parts of a prompt and keep no source code.

### The Prompt-Weight Panel

When a traffic store is available, the dashboard shows one more panel:

```text
Prompt weight (proxy)  — averages per request, this run
ROLE                   REQS   AVG REQ   MAX REQ    TOOLS   SYSTEM     MSGS  MSG%
architect                36    117.5k    208.2k    33.3k     5.0k    56.3k   48%
coder                    73    203.6k    366.8k    33.8k     5.9k   133.2k   65%
human-in-the-loop        18    211.1k    250.3k   136.6k    30.7k    43.9k   21%
refactorer               93    139.1k    242.3k    33.9k     5.2k   108.6k   78%
specifier                12     91.3k    183.9k    28.5k     5.4k    22.9k   25%
```

The panel shows the current run only. The store stays after a run. An average of more than one
run mixes configurations that you cannot compare. The flag `--traffic-all-history` shows all the
runs. The heading always tells you which data you look at. A column shows `-` when no data is
available. It does not show `0`, because `0` is incorrect.

### What the Proxy Found

The panel above is not a theory. These are measurements of true runs:

- **The tool schemas were 81% of a small request.** The function `build_agents_payload` read the
  `tools` list of each worker but sent nothing. Thus each worker received the full default set
  of tools. Kiln now sends the list. The quantity of tools decreased from 30 to 9. The tool bytes
  decreased from 98.4k to 33.2k. The **tokens for each request decreased by 40%**, from 71.9k to
  43.4k. The traffic for one cycle decreased from 43.4 MB to 24.0 MB.
- **The worker instructions are 3% to 5% of a request.** This is the `SYSTEM` column. Thus a
  decrease of the size of the `*-worker.md` files has almost no effect. The `MSGS` column is 60%
  to 78%.
- **96.8% of all input tokens are cache reads.** The total was 11.5M tokens. Only approximately
  370k tokens had a charge as new tokens. Thus a decrease of the cached part gives much less than
  its size shows.
- **The duplication in one request is 0.3% to 2.4%.** The workers read almost nothing two times.
  The increase of the conversation is the cost of the work. It is not an error.

### How to Use the Proxy to Optimize *Your* Project

The measurements above apply to the scaffolding that Kiln supplies. But the constitution, the
roles, the skills and the worker files that operate are **yours**. Kiln copies `kiln/project/`
into your repository so that you can change it.

The proxy tells you which changes have value. Kiln measures. You decide what to make smaller,
because this is a question about your project.

Use this procedure:

1. **Run a cycle with `--proxy`.** Then read the prompt-weight panel. The `SYSTEM` column is your
   constitution and your role file. The `TOOLS` column is the tool set of your worker. The `MSGS`
   column is the conversation.
2. **Change the largest column.** Do not change the column that is easiest to edit. These are
   different, and the measurements above show the danger.
3. **Make one change. Then measure again** with work of the same type.

This table shows where the controls usually are:

| If the big column is… | The lever is… |
|---|---|
| `TOOLS` | the `tools:` list in your worker frontmatter — declare only what the role needs |
| `SYSTEM` | your `constitution/` and `roles/` files, but see the warning below |
| `MSGS` | how much work you give a role per handoff, and how much file content it must read |

**Warning: two errors are easy to make.**

The first error is to make the instructions smaller. This is the usual first idea, and it is
usually incorrect. The `SYSTEM` column was 3% to 5% of a request. Thus a decrease of one half
changes almost nothing. But you lose rules that the workers obey. Look at the column before you
change a file.

The second error is to look only at the prompt weight. Also look at the **`CACHE` column in the
dashboard**. At a hit rate of 97%, a decrease of the *cached* part gives much less than its size
shows. And a change that makes the cache invalid at each cycle can cost more than it saves. A
role with a low value in `CACHE` is more important than a role with a large value in `AVG REQ`.

**Note on correct measurement:** Kiln cannot do the same work two times with two different
configurations. Thus a comparison of two runs is a comparison of different work. Use one
comparison as an indication only. It is not proof.

Do the comparison again, or make only changes with a large effect. The 40% decrease of the tool
schemas was sufficiently large. A decrease of 5% with the same method would not be sufficiently
large.

---

## Constitution and Roles

This is the recommended layout of a project:

```text
kiln/
  project/
    roles/
      architect.md             # Architect role (design review, approval)
      coder.md                 # Coder role (TDD implementation)
      refactorer.md            # Refactorer role (quality gates, refactoring)
      specifier.md             # Specifier role (Gherkin acceptance tests)
      reviewer.md              # Reviewer role (batch review alternative to refactorer)
      human-in-the-loop.md    # Human-in-the-loop role (human-facing intake and approval checkpoint)
    constitution/
      workflow.md              # Handoff protocol, branch discipline, queue format
      engineering.md           # Language, tools, dependencies, practices
      project.md               # Project-specific architecture, tech stack, quality gates

# Optional: Override default profiles (framework uses kiln/framework/profiles.json)
kiln.profiles.json           # Project-specific profiles (optional, at root)
```

**Note:** A project gets the configuration profiles from the framework file
`kiln/framework/profiles.json`. To use different profiles, make the file `kiln.profiles.json` in
the project root.

### How Kiln Finds a Profile

A configuration profile specifies the agents, their roles and their locations. Kiln looks in
these locations in this sequence. Kiln uses **the first file that it finds**:

1. **The project root** — `kiln.profiles.json`.
2. **The project configuration** — `kiln/profiles.json`. Kiln looks here, but the init command
   never makes this file. Do not confuse this file with the directory `kiln/project/`, which
   holds the constitution, the roles and the skills.
3. **The project state** — `.kiln/profiles.json`. Kiln looks here, but the init command never
   makes this file.
4. **The framework** — `kiln/framework/profiles.json`. These are the default profiles for all
   projects.
5. **The home directory of the user** — `~/.kiln/profiles.json`. This file is optional.
6. **The system** — `/etc/kiln/profiles.json` on Unix, or
   `C:\ProgramData\kiln\profiles.json` on Windows. This file is optional.

The first file that Kiln finds has full control. Kiln does **not** merge the files from
different locations.

**Note:** An earlier version of this document said that Kiln does not use locations 2 and 3.
That was incorrect. Kiln looks in these locations. Thus a file in one of them replaces the
default profiles.

All projects use the framework file `kiln/framework/profiles.json` as the default. That file
specifies the standard workflow with four agents: specifier, coder, refactorer and architect.
Thus a new project operates immediately with no configuration.

**To use different profiles in one project,** make the file `kiln.profiles.json` in the project
root.

> ⚠️ **Warning:** That file **replaces** the profiles of the framework. It does not add to them.
> There is no `extends` mechanism. When `kiln.profiles.json` exists, the profiles `default`,
> `codex-only` and `mixed-backends` are no longer available. To keep them, copy them into your
> file. First copy `kiln/framework/profiles.json`, then make your changes. Do not write a new
> file that has one profile only.

### The Layers of the Constitution

- **`constitution/workflow.md`** specifies the handoff protocol, the rules for the git
  worktrees, and the rules for the messages between the agents.
- **`constitution/engineering.md`** specifies the language, the build tools, the test
  frameworks, the quality tools and the coding practices.
- **`constitution/project.md`** holds the rules for one project: the language, the limits of the
  architecture, and the quality values. Kiln makes this file from a template. Complete it for
  your project.

The example projects keep their detailed technical rules in their `README.md`. An example can
also supply its own `project.md` and `engineering.md` in
`examples/<name>/kiln/project/constitution/`. The flag `-Example <name>` copies these files over
the default files. For example, `library-hub-java` replaces both files. The new files specify
the Maven and Spring tools in the place of the Python defaults of the framework.

**How Kiln makes the instructions for an agent:** Kiln always combines the constitution and the
role instructions at start.

- The constitution files `workflow.md`, `engineering.md` and `project.md` give the common rules
  to all agents.
- The role file `roles/<role>.md` gives the instructions for one role.
- Kiln merges the two into one instruction file for each agent:
  - **Claude agents** get `CLAUDE.md` in the root of the worktree.
  - **Copilot agents** get `.github/copilot-instructions.md` in the root of the worktree.
  - **Codex agents** get `AGENTS.md` in the root of the worktree. This is the convention of the
    Codex CLI. A test of the string table of the installed program shows this.
  - **Grok** gets no instruction file. Grok has no wrapper mode. It has the scheduler path only.
    The scheduler reads the worker definition directly. Refer to **Known Limitations**.

Thus each agent has the full constitution and its own role instructions.

**How Kiln makes the worker subagent for an `auto` role:** each of these agents is a thin,
continuous **wrapper**. The wrapper only listens, merges, commits and hands off.

At each cycle, the wrapper sends the true work to a temporary **worker**. Kiln makes the worker
from the role file `roles/<role>.md`, the file `engineering.md` and the file `project.md`. Kiln
does **not** use `workflow.md`. The handoff protocol is the function of the wrapper. It is not
the function of the worker.

The coder is an example. The wrapper receives the handoff and merges it. Then the wrapper sends
the work to a new `coder-worker` subagent. That worker does the TDD cycle: red, then green, then
refactor. Then the wrapper sends the result to the next role.

![Coder wrapper internal cycle: receive and merge, delegate to coder-worker, retry once on failure, then handoff — with the worker's TDD red/green/refactor loop shown alongside](docs/images/diagram-coder-internal-cycle.svg)

*The wrapper (on the right) is the same for each role. Only the loop of the worker (on the left)
is different. A refactorer-worker does the coverage gate, the CRAP gate and the mutation gate in
the place of the TDD cycle.*

Each backend sends the work to its worker in a different way:

- **Claude:** Kiln writes the worker to `.claude/agents/<role>-worker.md`. The wrapper uses the
  `Agent` tool of Claude Code. The call blocks and is deterministic, because the wrapper gives
  `subagent_type: "<role>-worker"`.

  The worker cannot use the `Agent` tool. Thus it cannot make more subagents. The worker also
  has no MCP messaging tools. It can only read, write, edit and test in its worktree.

  The full transcript of the worker never goes into the context of the wrapper. Only the final
  report goes there. Thus the context of the wrapper stays small at each cycle. It does not fill
  with the data of the implementation work.
- **Copilot:** Kiln writes the worker to `.github/agents/<role>-worker.agent.md`. This is the
  custom-agent format of the GitHub Copilot CLI. The wrapper sends the work with an instruction
  in prose. The loop template tells the wrapper to use the named custom agent. Then the harness
  of the Copilot CLI makes the subagent call with its own context window.

  The `tools:` field has the values `read`, `write` and `shell`. It lists no MCP server. Thus
  the worker has no messaging access. This is the same isolation as the Claude worker.

  **Note:** The Claude `Agent` tool always makes the call. The Copilot delegation is different.
  The model decides. GitHub made the Copilot CLI more selective about delegation. Thus the
  prompt of the wrapper tells it to always delegate. It must delegate even when it thinks that
  it can do the work more quickly.
- **Codex:** Kiln writes the worker to `.codex/agents/<role>-worker.toml`. This is the
  custom-agent format of the Codex CLI. The necessary fields are `name`, `description` and
  `developer_instructions`. The official documentation at `developers.openai.com/codex/subagents`
  gives these fields.

  The wrapper uses the multi-agent tools of Codex: `spawn_agent`, `assign_agent_task`,
  `wait_agent` and `close_agent`. This is the `multi_agent` function. It is stable and it is on
  by default. A test against an installed `codex.exe` shows this.

  The value `mcp_servers = {}` in the TOML file removes the messaging access. This is the same
  isolation as the Claude worker and the Copilot worker.

### The Default Workflow

The default workflow has four agents and operates in a continuous loop.

For each Claude wrapper agent, Kiln makes a `CLAUDE.md` file. That file has the role file and a
**loop template**. The loop template controls the cycle with two skills, `/kiln-receive` and
`/kiln-handoff`. The skills are in `kiln/project/skills/kiln-receive` and
`kiln/project/skills/kiln-handoff`. Between the two skills, the wrapper sends the work to the
worker subagent of the role.

The cycle has five steps:

1. **`/kiln-receive`** calls `wait_for_message()` with the `kiln-channel` MCP server. The call
   blocks until a handoff comes. Then the skill writes the message to `tmp/handoff-in.md`, which
   is necessary because an auto-compact can erase the context. Then the skill merges the commit
   of the sender with `git merge <commit>`. Then the skill writes a `[RECEIVED]` line to
   `logbook.md`.
2. **Send the work to the worker.** The wrapper implements nothing. The wrapper calls the
   `Agent` tool with `subagent_type: "<role>-worker"`. The call blocks. The wrapper gives the
   content of the handoff, the branch and the worktree. The worker does the work of the role.
   Then the worker reports what it did.

   **Note:** The `specifier` role is different. It operates in `manual` mode and needs the
   approval of the user. It is not yet part of this pattern. Refer to **Known Limitations**.
3. **Do the work again, or escalate.** If the worker reports that it could not complete the
   work, the wrapper calls it one more time. The wrapper gives the failure as feedback. If the
   worker fails a second time, the wrapper sends a handoff that reports the problem. Thus the
   cycle does not stop without an indication.
4. **`/kiln-handoff`** writes a `[SENT]` line to `logbook.md`. Then it squashes the commits of
   the work into one commit. Then it puts the handoff into `.kiln/messages.db` with the `query`
   tool. Then it reads the row again to make sure that the row is there. If the row is absent,
   the skill writes it again.
5. **Go to step 1 immediately, in the same turn.** A handoff that Kiln sent and verified is not
   the end of the cycle. The loop template says that the turn is not complete until
   `/kiln-receive` operates again.

   **Note:** Step 5 corrects a failure that a live test found. An agent completed a verified
   handoff. Then the agent stopped. It did not wait for the next message.

**Copilot uses the same sequence:** receive, delegate, do again one time after a failure, hand
off, then loop in the same turn. But Copilot uses its own loop in `loop-auto-copilot.md`. It
examines the `messages` table directly with SQL through the `query` tool, because Copilot has no
`kiln-channel` MCP tool that blocks. It squashes the commits and writes the log in the loop, not
in a shared skill file.

**Codex also uses the same sequence** with `loop-auto-codex.md`. But Codex is not the same as
Copilot. Codex uses the skills `/kiln-receive` and `/kiln-handoff`, because the Codex CLI
supports slash commands. Codex sends the work to the `<role>-worker` custom agent with its
multi-agent tools: `spawn_agent`, `assign_agent_task`, `wait_agent` and `close_agent`. It does
not use the `Agent` tool of Claude Code. It does the work again one time after a failure, in the
same way.

Codex also has a `manual` mode with the file `loop-manual-codex.md`. Use it for a role with a
human supervisor, such as `specifier`. All the other backends have the same function.

The sequence of the cycle is: **specifier → coder → refactorer → architect → specifier**

- **`specifier`** operates in **manual** mode. At start, it asks the user which function to
  specify. Then it writes Gherkin acceptance tests. Then it waits for the approval of the user.
  Then it sends the handoff to the coder. All the other roles operate in **auto** mode and need
  no approval.
- **`coder`** implements the behavior in small parts. It uses strict TDD until all tests pass.
  Then it sends a handoff to the refactorer.
- **`refactorer`** does the quality gates in this sequence: coverage, then CRAP, then DRY, then
  the count of the mutation sites. Then it refactors the code to make tests easier. Then it
  sends a handoff to the architect.
- **`architect`** examines the structure of the modules. Then it does the verification before
  the handoff: mutation, then DRY, then a soft Gherkin check. Then it sends the completion
  report to the specifier.

> **Optional role:** `reviewer` is an alternative to `refactorer`. It gives more attention to
> batch processes and review pipelines. To use it, put it into your profile in
> `kiln/framework/profiles.json`. Refer to `kiln/project/roles/reviewer.md`.
>
> **Optional role:** `human-in-the-loop` is a checkpoint for a human before the cycle. Use it in
> a profile where `specifier` operates in `auto` mode with no user.
>
> The **`default` profile** of the framework uses this role with an autonomous specifier. The
> role `human-in-the-loop` operates in manual mode in `@current`. It collects a request and
> confirms it with the user. Then it gives the request to `specifier`, which now operates in
> `auto` mode in its own worktree. The specifier does its usual Gherkin workflow with no user.
> At the end, the completion report of the architect goes back to `human-in-the-loop` for the
> user. Refer to `kiln/project/roles/human-in-the-loop.md` and to the section "Auto-Mode Worker
> Entry Point" in `kiln/project/roles/specifier.md`.

---

## Running Kiln

### Quick Reference

| Platform | Command |
|---|---|
| **Windows** | `.\bin\kiln.ps1 -WorkingDir .` |
| **Unix/macOS** | `./bin/kiln.sh .` |

Both shims send each argument to the same Python CLI. Thus **all flags operate on both platforms
in each spelling**. The three forms `-ProfileName mixed-backends`, `-Profile mixed-backends` and
`--profile mixed-backends` are the same flag.

| Flag | Aliases | Effect |
|---|---|---|
| `--working-dir <path>` | `-WorkingDir`, `-Target`, `--target` | Project directory (default: `.`) |
| `--profile <name>` | `-Profile`, `-ProfileName` | Which profile to launch |
| `--terminal <backend>` | `-Terminal` | `wezterm`, `wt`, `tmux` or `none` (default: auto-detect) |
| `--stop` | `-Stop` | Stop a running swarm |
| `--list-profiles` | `-ListProfiles` | List available profiles and exit |
| `--init` | `-Init` | Scaffold a new project instead of launching (or the `init` subcommand) |
| `--example <name>` | `-Example` | Seed the scaffold from `examples/<name>` |
| `--no-git` | `-NoGit` | Skip git initialisation when scaffolding |
| `--dry-run` | | Print what would launch, start nothing |
| `--proxy` | | Route agent API traffic through the local capture proxy (off by default) |
| `--proxy-port <n>` | | Pin the capture proxy to an exact port (default: `8787`, probed upward if busy) |
| `--capture <mode>` | | `metadata` (default) or `full` — capture depth, see "Traffic Capture" |
| `--verbose` | `-Debug` | Verbose output |

If no git repository exists, Kiln makes one. Then Kiln makes the worktrees and starts the
agents.

Use `--dry-run` to see what a profile does. It shows the command line and the working directory
for each role. It starts no terminal.

### Windows (PowerShell)

1. **Make a new project** from the root of the Kiln repository:

   ```powershell
   .\bin\kiln.ps1 -Init -WorkingDir C:\path\to\my-project
   cd C:\path\to\my-project
   ```

   This command makes the project with all the necessary files: the constitution, the roles, the
   tools and the git repository.

2. **Optional: include an example brief.** The examples are `library-hub`, `library-hub-java` and
   `battlezone`. Refer to **Examples**.

   ```powershell
   .\bin\kiln.ps1 -Init -WorkingDir C:\path\to\library-hub -Example library-hub
   ```

   This command puts the `README.md` of the example into your project as the brief. Thus the
   agents know immediately what to build.

3. **Start Kiln**:

   ```powershell
   .\bin\kiln.ps1 -WorkingDir .
   ```

   You can also control the profile and the terminal:

   ```powershell
   # Run the default 'default' profile (human-in-the-loop intake feeding an autonomous specifier -> coder -> refactorer -> architect cycle)
   .\bin\kiln.ps1 -WorkingDir .

   # Run a different profile (e.g., 'mixed-backends' to validate multiple agent backends at once)
   .\bin\kiln.ps1 -WorkingDir . -ProfileName mixed-backends

   # Use Windows Terminal instead of WezTerm (default)
   .\bin\kiln.ps1 -WorkingDir . -Terminal wt

   # Enable debug mode (verbose output for troubleshooting MCP issues)
   .\bin\kiln.ps1 -WorkingDir . -Debug

   # Kill orphaned MCP server processes after closing the terminal
   .\bin\kiln.ps1 -Stop
   ```

4. **The start procedure makes these items:**
   - The git worktrees in `.worktrees/`, one for each role that is not `@current`.
   - An instruction file in each worktree. Claude agents get `CLAUDE.md`. Copilot agents get
     `.github/copilot-instructions.md`. The file holds the constitution, the project rules and
     the role.
   - A worker agent definition for each `auto` role. Claude gets
     `.claude/agents/<role>-worker.md`. Copilot gets `.github/agents/<role>-worker.agent.md`.
     The wrapper sends its work to this worker at each cycle.
   - A `.mcp.json` file in each worktree, with `kiln-db` and `kiln-channel`. The file has the
     correct role and branch in its environment variables.
   - The channel logs at `.kiln/logs/channel-<role>.log`.
   - The Claude Code debug logs at `.kiln/logs/claude-debug-<role>.log`. The flag is
     `--debug-file`. Use these logs to find the cause of a stop.
   - The WezTerm tabs and panes, or the Windows Terminal tabs, for each role.
   - The SQLite database `.kiln/messages.db` for the messages between the agents.

5. **Do a check.** Each tab of an agent shows a prompt. Send the command `pwd` to the agent. The
   answer must be the correct worktree.

### Unix/macOS (zsh)

1. **Make a new project** from the root of the Kiln repository:

   ```sh
   ./bin/kiln.sh init /path/to/my-project
   cd /path/to/my-project
   ```

   This command makes the project with all the necessary files: the constitution, the roles, the
   tools and the git repository.

2. **Optional: include an example brief.** The examples are `library-hub`, `library-hub-java` and
   `battlezone`. Refer to **Examples**.

   ```sh
   ./bin/kiln.sh init /path/to/library-hub --example library-hub
   ```

   This command puts the `README.md` of the example into your project as the brief. Thus the
   agents know immediately what to build.

3. **Start Kiln**:

   ```sh
   ./bin/kiln.sh .
   ```

4. **Kiln then does these tasks:**
   - It makes the git worktrees in `.worktrees/`.
   - It starts the tmux sessions, one for each role.
   - It makes the Terminal.app windows or the WezTerm tabs. Kiln finds which one to use.
   - It gives a tmux pane to each agent.
   - It makes the `CLAUDE.md` files with the full constitution and the role.
   - It makes `.claude/agents/<role>-worker.md` for the Claude agents.

   **Note:** A test on Windows shows that the receive-delegate-handoff loop operates. There is no
   equivalent test on Unix. Refer to **Known Limitations**.

---

## Configuration Profiles

Kiln uses JSON profiles to specify the topology of the swarm. The default profile has the name
`default`. The top-level key `"default"` in `kiln/framework/profiles.json` sets that name. The
function `load_profile()` in `kiln/framework/launcher/config.py` reads the key at start, when you
give no `--profile` flag.

All projects get the default profiles from `kiln/framework/profiles.json` automatically.

**To use different profiles in one project,** make the file `kiln.profiles.json` in the project
root. Kiln then uses your profiles in the place of the framework profiles.

### The Default Profile of the Framework

The `default` profile has one role for the human and four autonomous roles. The autonomous cycle
is specifier, coder, refactorer and architect.

The role `human-in-the-loop` operates in `manual` mode in the main directory (`@current`). It
collects a request and confirms it with you. Below it, an `inbox` pane shows the escalations.

The other four roles operate in `auto` mode **on the deterministic scheduler**. Each role has its
own worktree. They need no input from a human. A `dashboard` tab shows the full swarm.

For each `auto` role, the scheduler pane and the worker both use Sonnet. To put the pane on a
less expensive model than the worker, refer to **How to Use Different Models for the Wrapper and
the Worker**.

![Default profile topology: human-in-the-loop gathers and confirms a request, hands it to an autonomous specifier → coder → refactorer → architect cycle, which reports completion back](docs/images/agentic_coding_topology_human_left_v3.svg)

*The JSON below makes this configuration: one manual role for the human with an inbox pane for
the escalations, one autonomous cycle of four roles, and one dashboard tab. Refer to **Inbox
Mode** and **Dashboard Mode**.*

```json
{
  "profiles": {
    "default": {
      "description": "Human-guided request intake (human-in-the-loop, with a live inbox pane) feeding a fully autonomous specifier -> coder -> refactorer -> architect cycle on the deterministic scheduler, plus a dashboard tab",
      "terminals": [
        {
          "role": "human-in-the-loop",
          "agent": "claude",
          "worktree": "@current",
          "mode": "manual",
          "model": "claude-sonnet-5"
        },
        {
          "role": "inbox",
          "worktree": "@current",
          "mode": "manual",
          "scheduler": "inbox",
          "watches": "human-in-the-loop"
        },
        {
          "role": "specifier",
          "agent": "claude",
          "worktree": "specifier",
          "mode": "auto",
          "model": "claude-sonnet-5",
          "scheduler": "python"
        },
        {
          "role": "coder",
          "agent": "claude",
          "worktree": "coder",
          "mode": "auto",
          "model": "claude-sonnet-5",
          "scheduler": "python"
        },
        {
          "role": "refactorer",
          "agent": "claude",
          "worktree": "refactorer",
          "mode": "auto",
          "model": "claude-sonnet-5",
          "scheduler": "python"
        },
        {
          "role": "architect",
          "agent": "claude",
          "worktree": "architect",
          "mode": "auto",
          "model": "claude-sonnet-5",
          "scheduler": "python"
        },
        {
          "role": "dashboard",
          "worktree": "@current",
          "mode": "manual",
          "scheduler": "dashboard"
        }
      ],
      "layout": {
        "tabs": [
          {
            "title": "Human-in-the-Loop",
            "panes": [
              {"role": "human-in-the-loop"},
              {"role": "inbox", "direction": "Bottom", "size": 0.22}
            ]
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
          },
          {
            "title": "Dashboard",
            "panes": [{"role": "dashboard"}]
          }
        ]
      }
    }
  }
}
```

### The Other Supplied Profiles

The file `kiln/framework/profiles.json` also has two variants of the same topology. Each variant
uses a different backend for the roles that have an agent:

- **`codex-only`** puts each role with an agent on Codex, and this includes
  `human-in-the-loop`. Use it to test Codex fully, in wrapper mode and in scheduler mode.
- **`mixed-backends`** tests more than one backend at the same time. The roles `specifier` and
  `refactorer` use Codex. All the other roles use Claude.

  **Note:** Copilot is not in the scheduler-mode rotation now. Refer to
  [Known Limitations & Future Work](#known-limitations--future-work).

To use one of these profiles, give `-ProfileName <name>` on Windows or `--profile <name>` on
Unix.

**The fields of a terminal:**

- **role** points to the file `kiln/project/roles/<role>.md`. That file must exist.
- **agent** selects the AI tool: `claude`, `copilot`, `codex` or `grok`. All four have a
  scheduler adapter. Thus `"scheduler": "python"` operates with all four.

  **Caution:** Only `grok` has no *wrapper* mode. Thus a `grok` role must use `auto` mode with
  the scheduler. A `grok` role must never use `manual` mode. Refer to **Known Limitations**.
- **worktree** has the value `@current` for the main directory. Any other name makes the
  directory `.worktrees/<name>/`.
  - Use `@current` for the roles that coordinate or review on the current branch.
  - Use a separate name for each role that needs isolation. Then each agent has its own branch.
- **model** applies to Claude agents only. It selects the Claude model. Examples are
  `claude-haiku-4-5-20251001`, `claude-sonnet-5` and `claude-opus-5`.
- **workerModel** applies to Claude agents in `auto` mode only. It is optional. It sets a
  different model for the `<role>-worker` subagent. If you do not set it, the worker uses the
  model of the wrapper. This is the default behavior of Claude Code for a subagent with no
  `model` field.

**How to Use Different Models for the Wrapper and the Worker:** The continuous wrapper does three
things only: it listens, it delegates, and it sends. The wrapper never thinks about the task.
That is the function of the worker subagent.

Thus the wrapper can use an inexpensive and fast model, such as Haiku. The worker does the true
implementation work and can use a stronger model, such as Sonnet:

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

Kiln uses the `model:` field of a Claude Code subagent for this. When you set `workerModel`, the
function `write_worker_file()` in `kiln/framework/launcher/generate.py` writes
`model: <workerModel>` into the file `.claude/agents/<role>-worker.md`. In scheduler mode, the
module `worker_prompt.py` reads the same field to select the model of the one-shot worker.

Claude Code gets the model of a subagent from the file of that subagent. The model of the parent
session has no effect. Thus a wrapper on Haiku truly starts a worker on Sonnet.

In the `default` profile of the framework, the wrapper and the worker both use Sonnet for each
role. The field `workerModel` is absent, thus the worker uses the model of the wrapper. To get
the less expensive division, set `workerModel` for each role.

### Layout Configurations

The `layout` field specifies how the terminal shows the agents. Kiln has more than one type of
layout.

**Tabs layout** (the default):

```json
"layout": {
  "type": "tabs",
  "roles": ["specifier", "coder", "refactorer", "architect"]
}
```

Each role gets one tab.

**Grid layout** (2x2, 3x3 or a different size):

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

One tab shows all the agents in a grid at the same time.

**Split-pane layout:**

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

There are more tabs. Each tab has two panes, one beside the other.

**Focus layout** (one pane at the top, more panes below):

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

### A Different Agent for Each Role

You can use different agents in one swarm:

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
          "model": "claude-sonnet-5"
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
          "worktree": "architect",
          "mode": "auto",
          "scheduler": "python"
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

You must install the CLI tool of each backend. The tool must be on `PATH`.

**Caution:** The role `architect: grok` must have `"scheduler": "python"`. Grok has no wrapper
mode. Thus a grok role can only operate as a scheduled `auto` role. Refer to **Known
Limitations**.

The framework supplies a version of this idea that operates: the **`mixed-backends`** profile.
In it, `coder` uses Copilot, `refactorer` uses Codex, and the other roles use Claude. All roles
use the scheduler. Refer to **The Other Supplied Profiles**.

### How to Run a Different Profile

To start a specific profile, use the flag `-ProfileName` on Windows or `--profile` on Unix:

```powershell
# Windows
.\kiln.ps1 -WorkingDir . -ProfileName staging
```

```bash
# Unix/macOS
./kiln.sh . --profile staging
```

If you give no profile, Kiln uses the `default` profile of the framework. The argument for the
working directory is always necessary.

### Branch Names for Gitflow

A sub-branch has the name `<current-branch>-<worktreeName>`. Thus Kiln uses the namespace of the
active branch. On the branch `feature/ABC123`, the names are `feature/ABC123-coder`,
`feature/ABC123-refactorer` and so on. On the branch `main`, the names are `main-coder`,
`main-refactorer` and so on.

**A role with the worktree `@current`** operates directly on the current branch in the main
directory. It makes no sub-branch.

**Caution:** A sub-branch is local. You cannot push it. A git pre-push hook prevents this. A push
of a sub-branch fails. This is the intended behavior. A sub-branch belongs to the orchestration
and is temporary.

---

## Terminal Behavior

Kiln opens the terminal windows and the tabs with a small adapter for each terminal backend.

### How Kiln Finds the Terminal (All Platforms)

There is one rule. The function is `detect_backend()` in `launcher/terminals/__init__.py`. Kiln
does the same steps on each operating system until the last step:

1. If you gave the flag `--terminal <backend>`, Kiln uses that backend.
2. If the variable `KILN_TERMINAL` has a value, Kiln uses that backend.
3. If the variable `WEZTERM_PANE` has a value, and `wezterm` is on `PATH`, Kiln uses WezTerm.
   This condition shows that you are already in a WezTerm pane.
4. If `wezterm` is on `PATH`, Kiln uses WezTerm. This applies to each operating system. Thus
   WezTerm on Linux and macOS operates with no extra configuration.
5. If not, Kiln uses the alternative for the platform. On Windows, this is Windows Terminal
   (`wt.exe`). On Unix, Linux and macOS, this is tmux, if tmux is installed.
6. If Kiln finds nothing, it uses `none`. Kiln then shows the commands and starts nothing.

### How to Select a Different Terminal

Set the variable `KILN_TERMINAL` to a specific backend:

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

You specify the layout in your profile. A layout can be simple, with tabs only. A layout can
also be complex, with splits, grids or a focus arrangement.

#### The Types of Layout

**Tabs layout** (the default):

- Each agent gets one terminal tab. Four agents get four tabs.
- Each tab operates independently and has its own colors.
- The division between the roles is clear.
- You can select an agent with the mouse quickly.
- Use this layout when you want a simple division.

**Grid layout** (2x2, 3x3 or a different size):

- One window shows all the agents at the same time.
- You set the rows and the columns, for example `gridRows: 2, gridCols: 2`.
- The view is compact. You can look at all the roles.
- This layout is good for quick coordination.
- Use this layout when you want to see all the agents, and the quantity of roles is small.

**Split-pane layout:**

- There are more tabs. Each tab has its own arrangement of panes.
- For example: tab 1 has the specifier and the coder, one beside the other. Tab 2 has the
  refactorer and the architect.
- This layout balances the view and the organization.
- Use this layout when you want to put related agents together.

**Focus layout** (one role at the top):

- The top tab shows one role at the full height.
- The bottom tab shows the other roles in more than one pane.
- For example: the specifier at the top, and the coder, the refactorer and the architect below.
- Use this layout when you want to look at one agent and monitor the others.

All the layouts operate on **WezTerm**, on each platform. The `layout` field of the profile
controls the grid, the split and the focus arrangements. The generated Lua of WezTerm reads that
field directly.

**Windows Terminal** makes an approximation of the tabs and the simple splits with
`wt.exe split-pane`. It does not read the `direction` and `size` values for each pane. Thus a
grid layout or a focus layout becomes a generic alternating split. It is not the arrangement
that you specified.

**tmux does not read the `layout` field at all.** Each role becomes an independent detached
session with the name `kiln-<role>`. This occurs with each layout in the profile. Refer to
**tmux Behavior**.

### WezTerm Configuration

Kiln makes a WezTerm configuration file at start. That file makes the multi-agent layout. Kiln
does this when it uses WezTerm, which is the default when `wezterm` is on `PATH`.

**Important:**

- When you run `kiln.ps1`, Kiln writes a `~/.wezterm.lua` file in your home directory.
- The file has your agents and your layout.
- **Kiln first copies your `~/.wezterm.lua`** to `~/.wezterm.lua.kiln-backup`.
- **Kiln puts the copy back** approximately 500 ms after WezTerm starts, when Kiln sees that the
  window is open.
- Thus your own configuration stays. The Kiln configuration is temporary and applies to one
  session.

**If the procedure fails:** To put your configuration back manually, run this command:

```powershell
Move-Item ~/.wezterm.lua.kiln-backup ~/.wezterm.lua -Force
```

### The Live Status of an Agent (WezTerm)

The wrapper of each `auto` role has four states:

- **waiting** — the wrapper does nothing and waits for the next message.
- **receiving** — a message came. The wrapper writes it to disk, merges it and logs it, before
  it delegates the work.
- **delegating** — the worker operates and does the true work.
- **handoff** — the wrapper sends the result.

At each change, the wrapper runs
`python .kiln/tools/set-status.py <role> <state> [detail]`. That command writes two items:

- **The file `.kiln/status/<role>.json`** with the fields `role`, `state`, `detail`, `since` and
  `title`. This file is always correct. You can read it on each platform and in each terminal.
- **An OSC sequence with the title of the terminal.** This is not reliable. The agent CLI in the
  same pane writes its own title at each frame, for the spinner or the idle icon. The CLI writes
  more frequently. Thus the CLI usually wins.

In WezTerm, the generated Lua configuration reads the JSON files directly. It does not read the
title of the pane, because two programs write that title. The Lua reads the files approximately
one time each second. Then it shows a status bar with colors in the top right corner of the
window.

The bar has one badge for each role. The color of the background shows the state: green for
waiting, blue for receiving, blue-green for delegating, and violet for handoff. The bar stays
visible when a different tab or pane has the focus.

Thus you can see the state in a grid layout also. In the "Autonomous Cycle" tab of the default
profile, more than one role uses one tab. In that condition, a role has no title of its own.

![Live status bar in the top-right of a WezTerm window, showing human-in-the-loop as "handoff" and specifier as "delegating: specifier-worker" while coder, refactorer, and architect show "waiting"](docs/images/kiln4.png)

*The badge of the specifier during a cycle: `delegating: specifier-worker`. The wrapper started
its worker subagent and waits for the result.*

Windows Terminal and tmux have no equivalent function for a status bar. But you can read the
JSON files directly. On Windows, use `Get-Content .kiln/status/coder.json`. On Unix, use
`cat .kiln/status/coder.json`.

This is one of the two functions that you lose when you do not use WezTerm. The other function
is the correct layout. Refer to **Layout Examples**.

> **Note:** Before the first test on Linux, this was not fully true. The program
> `set-status.py` found the project with the variable `KILN_PROJECT_DIR`. Only the WezTerm
> backend set that variable. Thus each write failed with tmux and with Windows Terminal, and
> `.kiln/status/` stayed empty. The STATE column of the dashboard also stayed empty.
>
> The program now finds the project root from its own location,
> `<project>/.kiln/tools/set-status.py`. Thus it writes the JSON with each backend.

**A scheduler role has more states:** `starting`, `waiting`, `receiving`, `working`, `retrying`,
`handing-off`, `idle`, `blocked` and `halted`. It uses the same `set-status.py` command. Thus
the WezTerm badges operate in the same way.

The colors show how much attention a state needs. They do not show only "green is good" and
"red is bad". Green, blue-green and blue are the normal cycle. The state `working` is in this
group deliberately, because an operator wants to see it. Amber (`retrying`) shows a small
problem that Kiln can correct. Then `blocked`, `escalated` and `halted` go from amber-red to
red, as the problem becomes larger.

The full table is `STATE_COLORS_HEX` in `kiln/framework/scheduler/pane_status.py`. The badge and
the pane status bar both read that table.

### The Pane Status Bar (Scheduler Roles, Each Backend)

A scheduler pane also puts a status line with colors on its **bottom** row. The line shows the
role, the state, the count of the cycles, the total cost, the tokens, the target of the handoff
and the last summary:

```text
 SPECIFIER   ● working   cycle 3   $1.24   238.4k tok   → coder   wrote create_book.feature
```

This bar needs no scripting function in the terminal. Thus it operates everywhere. The WezTerm
badges are different.

Kiln draws the bar with a VT scrolling region. Thus the pane is still a normal terminal. You can
select text, copy text, paste text and use the scrollback. Only the last row is not available.

To remove the bar, use `--no-status-bar`. The bar also removes itself when the output goes to a
pipe and not to a terminal. It removes itself in a pane with less than six rows.

> **Note:** The bar is at the bottom for a technical reason. A terminal puts the old lines into
> the scrollback only when the scrolling region starts at row 1. A bar at the top needs a region
> that starts at row 2. The pane would then scroll but keep no history.

The cost value is the sum of `total_cost_usd` for each worker invocation of this pane. The agent
CLI reports this value. The sum includes the invocations after a failure. The value applies to
one pane. It returns to zero when the process starts again. It uses the list prices of the API.
Thus you must read it as a relative value, not as an invoice.

The token value is a total. Kiln keeps the division into input, output, cache read and cache
write separately. The dashboard shows that division, because the dashboard is sufficiently wide.

Kiln does not show the cost or the tokens when the value is zero. A role whose backend reported
no usage shows nothing. It does not report that it used nothing.

### tmux Behavior (Unix Only)

Each role gets its own detached session with the name `kiln-<role>`. Kiln makes the session in
the worktree of that role. Kiln sends the command of the agent with `send-keys`.

To connect to a session, use `tmux attach -t kiln-coder`. The flag `--stop` stops all the
sessions.

**Caution:** tmux does not read the `layout` field of the profile. The grid, split and focus
arrangements have no effect. Each role is always an independent session. Kiln runs one
`tmux new-session` for each role.

To get the layout that the `layout` field describes, install WezTerm. An example is the pair of
panes for `human-in-the-loop` and `inbox` in the `default` profile. WezTerm operates natively on
Linux and macOS. It reads the same `layout` field as on Windows. Thus there is no limitation on
Unix for the WezTerm path. The limitation is in the tmux path only.

> **Note:** Earlier versions used a socket for each project. They obeyed `base-index` and
> `pane-base-index`. They also ran a watchdog that opened a closed window again. **The Python
> port has none of these functions.** The module `terminals/tmux.py` is minimal, deliberately.
> The watchdog is in the git history as `lib/kiln-window-watchdog.sh`.

### How to Add a Terminal Backend

The backends are in `kiln/framework/launcher/terminals/`. There is one module for each backend.
A backend receives a list of panes. The backend must only start each command in its own surface:

```python
# kiln/framework/launcher/terminals/mybackend.py
from . import PaneSpec

def launch(panes: list[PaneSpec], layout: dict | None, dry_run: bool = False) -> list[str]:
    """Start every pane. Returns the command(s) that were (or would be) run."""
```

A `PaneSpec` has the fields `role`, `name`, `path` (the worktree), `cmd` (already correct for
the shell), `mode` and `agent`.

To register the module, edit `terminals/__init__.py`. Add a constant to `VALID_BACKENDS`. Then
add a branch in `launch()`.

Obey these two conventions. Experience shows that they are necessary:

- **Obey `dry_run`.** Return the command and start nothing. Each backend has a test through this
  path. Thus no test opens a true terminal.
- **If your backend sends the command to a live shell,** use `clear=True` when you make the
  command. WezTerm (`send_text`) and tmux (`send-keys`) do this. The program `wt.exe` does not.
  Then the pane does not open with the command on the screen. Refer to `build_panes()` in
  `launcher/cli.py`.

---

## Cleanup

There are two levels. Their results are very different.

### How to Stop a Swarm (All Platforms)

```powershell
.\bin\kiln.ps1 -Stop          # Windows
./bin/kiln.sh --stop          # Unix/macOS
```

This command stops the processes that the swarm started: the schedulers, the MCP servers and the
tmux sessions. Kiln finds them by their command lines.

**The command makes no change to your files, your worktrees or your branches.** It does not
close the terminal window. Close the window yourself. If you do not, its panes show dead
prompts.

For a normal run, it is sufficient to close the window. The panes stop with it.

**Caution: `--proxy` is the exception.** The capture proxy is detached. Thus it continues after
the launcher stops, and it continues after you close the window. It stays as a background
process on its port.

But nothing fails if you close the window. **At the next `--proxy` start of that project, Kiln
stops the old proxy** before it starts a new one. Thus the port does not change, and the
quantity of proxies does not increase.

The `--stop` command is the correct way to end a run. It is also the only way to stop the proxy
immediately.

**Caution:** The `--stop` command applies to the full machine. This is the intended behavior. If
you run it in one project, it stops *each* Kiln process, and this includes the swarm of a
different project. The procedure at start is different: it only stops a proxy that writes to the
project that you start.

### Full Project Reset (Windows Only)

```powershell
.\bin\kiln-cleanup.ps1 -ProjectDir <path-to-project>
```

**Warning: This command erases data.** It removes these items:

- The git worktrees in `.worktrees/` and their branches.
- The swarm state in `.kiln/`.
- The generated instruction files `CLAUDE.md` and `.github/copilot-instructions.md`.
- The generated worker agent files `.claude/agents/*-worker.md` and
  `.github/agents/*-worker.agent.md`. The command keeps the custom agents that a person wrote.
- The `.mcp.json` file in the root, which Kiln makes for the `@current` roles.
- The git hooks for the swarm.
- The records of the terminal windows and tabs.

**Note:** The cleanup is optional and manual. It operates only when you call it. Thus you have
full control. You can examine your project before you erase it.

> **Known problem:** There is no equivalent of the full reset for Unix. The file
> `bin/kiln-cleanup.sh` existed, but it did not operate. The problem started before the port to
> Python. The file read `bin/terminal-adapter.sh`, and that path never existed, because the file
> was in `lib/`. Thus the script stopped immediately with `set -euo pipefail`.
>
> The file is removed. A file that looks like a function but does not operate is worse than no
> file. A port of `kiln-cleanup.ps1` to Python would correct the problem on both platforms.

---

## Examples

The repository has example project briefs in `examples/`. Use them with the flag
`-Example <name>` or `--example <name>`. The value `<name>` is a directory in `examples/`.

An example directory can have these files:

- `README.md` — the project brief. Kiln copies it into the root of the new project.
- `kiln/project/constitution/*.md` — optional replacement files, such as `project.md` and
  `engineering.md`. Kiln copies them over the default files. Thus an example can point the
  agents at its own tools, and not at the generic rules of the framework.

### LibraryHub (Python and FastAPI)

The file `examples/library-hub/README.md` has the brief. LibraryHub is a project of FastAPI
microservices. It has a hexagonal architecture. The services communicate with RabbitMQ events.
It has full quality gates for TDD and mutation testing.

The brief includes the rules for the architecture and the layers, the technical stack, the
quality gates and the test strategy. Thus the agents have the full technical context. This is
the reference implementation for Kiln.

**Windows:**
```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\my-library-hub -Example library-hub
```

**Unix/macOS:**
```bash
./bin/kiln.sh init /path/to/my-library-hub --example library-hub
```

### LibraryHub (Java and Spring Boot)

The file `examples/library-hub-java/README.md` has the brief. This example has the same domain,
the same contexts and the same user stories as LibraryHub. But it uses Java 21 and Spring Boot 3.
The components are Spring MVC, Spring Data JPA and Spring AMQP. It uses Maven with more than one
module. The test tools are JUnit 5, Cucumber-JVM, Testcontainers and jqwik. The quality tools are
JaCoCo, PIT, Checkstyle and ArchUnit.

This example supplies its own `constitution/project.md` and `constitution/engineering.md`. These
files replace the Python defaults of the framework with the tools of this stack.

**Windows:**
```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\my-library-hub-java -Example library-hub-java
```

**Unix/macOS:**
```bash
./bin/kiln.sh init /path/to/my-library-hub-java --example library-hub-java
```

### BattleZone (Python and pygame — Not a CRUD Service)

The file `examples/battlezone/README.md` has the brief. This example is a new implementation of
the Atari tank game of 1980, for one player. The original game used vector graphics. This version
is a first-person wireframe tank simulator. It uses Python and `pygame`.

The shape of this project is deliberately different from LibraryHub. It is one real-time
application with a game loop at a fixed interval. It is not a group of networked services.

But it keeps the same discipline of layers. The `domain` and `application` layers hold the
simulation: the movement, the collisions, the AI, the 3D projection mathematics and the score.
These layers have full unit tests, mutation tests and property tests.

The pygame window, the input and the graphics are the one boundary to the environment. A person
tests this boundary manually. Automatic gates do not test it.

This example supplies its own `constitution/project.md` and `constitution/engineering.md`.

**Windows:**
```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\my-battlezone -Example battlezone
```

**Unix/macOS:**
```bash
./bin/kiln.sh init /path/to/my-battlezone --example battlezone
```

Each of these commands makes a complete project. The project has the brief and the replacement
constitution files. You can run it immediately.

---

## Communication Health Check (`/kiln-ping`)

When the swarm operates, you can make sure that the agents communicate correctly. You need no
special profile and no special role. Ask `human-in-the-loop` for a health check.

### How to Run the Check

In the tab of `human-in-the-loop`, write a request like this one:

```
Run a health check.
```

This request starts the `kiln-ping` skill. The skill sends a ping through the normal chain:
specifier, then coder, then refactorer, then architect. This is the same path as a true request.

Each role in the chain does not do its usual work. Each role adds one line to a trail. Then each
role sends the trail to its usual next role. The trail comes back to `human-in-the-loop`, in the
same way as a true completion report. Then Kiln shows it:

```
Kiln-Ping: true
Trail:
- human-in-the-loop (main)
- specifier (main-specifier)
- coder (main-coder)
- refactorer (main-refactorer)
- architect (main-architect)
```

The ping uses the true chain. Thus it tests the same message lifecycle
(`queued`, then `delivered`, then `processing`, then `processed`). It tests the same git merge
at each step. It tests the same routing rules. It is not a separate test path.

### Requirements

You need a profile with a `manual` role in `@current` at the start of the chain. The `default`
profile of the framework has this. Thus you must configure nothing.

### How to Examine the Messages

The program `bin/kiln-db.ps1` on Windows does the usual queries. Thus you do not write SQL:

```powershell
.\bin\kiln-db.ps1 stats                     # message counts by status (queued/delivered/processed)
.\bin\kiln-db.ps1 list-messages specifier   # all messages for a role, optionally -Status <status>
.\bin\kiln-db.ps1 show-message <id>         # full content of one message
```

On each platform, you can also make the query directly:

```bash
sqlite3 .kiln/messages.db "SELECT status, COUNT(*) as count FROM messages GROUP BY status;"
grep "kiln-ping" logbook.md
```

### Troubleshooting

If the ping does not come back, do these steps:

1. **Examine the state of the agents.** Make sure that each configured agent operates. As an
   alternative, read `.kiln/status/<role>.json` for each role.
2. **Examine the MCP configuration.** Make sure that `.mcp.json` is in the Kiln directory of the
   project.
3. **Examine the console of each agent.** Each agent window shows what the agent received and
   what it did.
4. **Examine `logbook.md`.** Find the `[SENT]` and `[RECEIVED]` lines of the `kiln-ping` entries.
   These lines show where the ping stopped.
5. **Examine the log of the agent.** The file `.kiln/logs/claude-debug-<role>.log` shows what the
   agent did and decided. Use this file when the message queue and the channel log show no
   cause.

---

## Project Maturity and Status

### Kiln v0.3 — Phase 7: Python Core and Deterministic Scheduler

Phase 7 removed the two launchers, one in PowerShell and one in shell. One Python implementation
replaced them. Phase 7 also added the deterministic scheduler.

Each backend now has a scheduler adapter. Thus each `auto` role in each supplied profile uses
the scheduler.

**Note:** Phases 1 to 6 describe the wrapper architecture. That architecture is fully supported.
Each `manual` role still uses it. This is not a step in a sequence. It is structural: a live
conversation has no equivalent in the scheduler.

- ✓ **The Python launcher** (`kiln/framework/launcher/`). Approximately 3,200 lines of parallel
  PowerShell and shell became one implementation and approximately 95 lines of shim. All
  platforms now share the profile loading, the scaffolding, the worktrees, the generation, the
  terminal backends and the process teardown.
- ✓ **The deterministic scheduler** (`kiln/framework/scheduler/`). Refer to **Execution Modes**.
  The value `"scheduler": "python"` is the default for each `auto` role in each supplied
  profile. Wrapper mode stays for the `manual` roles and for a backend with no adapter. All four
  backends (`claude`, `copilot`, `codex` and `grok`) have an adapter in
  `kiln/framework/scheduler/adapters/`. A live test of each real CLI verified each adapter.
- ✓ **Conditional handoff routing.** The routing table in `workflow.md` has a new optional
  column, `When Sender`. Thus routing that depends on the sender is data. The wrapper and the
  scheduler can both obey it. Before, it was prose that only an LLM could interpret.
- ✓ **A status bar for each pane**, and a configuration banner for the scheduler roles.
- ✓ **The dashboard for the full swarm** (`"scheduler": "dashboard"`, `scheduler/dashboard.py`).
  This pane is like the `top` command. It collects the state of each role, the queue depth, the
  totals for the cost and the cycles, and the recent activity and escalations. It has its own tab
  in the `default` profile. Refer to **Dashboard Mode**.
- ✓ **The cost and the cycles stay on disk.** The file `.kiln/status/<role>.json` now has the
  optional fields `cycles` and `cost_usd`. The pane status bar writes them through
  `set-status.py`. Thus the values stay after the process stops. Any program that reads the
  status can use them.
- ✓ **The test suite.** It uses pytest over `launcher/` and `scheduler/`. The pure modules also
  have mutation tests. Run `pip install pytest ruff`, then run `pytest`. There is no install
  step.

  **Note:** The file `pyproject.toml` is a configuration for the tools. It is not a packaging
  manifest. The command `pip install -e .` cannot operate, because the file has no `[project]`
  table and no `[build-system]` table. The imports operate through
  `pythonpath = ["kiln/framework"]` in `[tool.pytest.ini_options]`.

**Live validation: one continuous run did the complete loop.** The first platform to do this was
Linux, not Windows.

```text
human-in-the-loop → specifier → coder → refactorer → architect → specifier → human-in-the-loop
```

The run had five scheduler cycles and a total cost of $2.52. There were **no escalations and no
stops**. The platform was Ubuntu 24.04 with WSL2. The workers were `claude` workers.

Each step did the true cycle: receive, then merge, then one-shot worker, then the `KILN-STATUS`
sentinel, then squash, then a verified insert of the handoff.

The last step is important. The specifier received the message of the architect. The specifier
correctly identified it as a report of a complete cycle. Then the specifier applied the row
`specifier | human-in-the-loop | architect` of the routing table. Thus the specifier returned the
message to the human. It did not send the message to the `coder` again.

That row of conditional routing closes the cycle. This was the first test of that row with true
agents. Before, only the unit tests examined it.

The `inbox` pane received the report. Then it did a squash merge into `main`. Then it set the
message to `processed`.

**Not yet tested:** more than one role with work at the same time on the SQLite queue. All five
cycles were sequential. Only one role had work at each moment. Thus the schedulers never
competed. The `mixed-backends` profile with all the backends at the same time is also not tested.

**Platform validation:** Linux (Ubuntu 24.04 with WSL2) is fully validated, and this includes
live agents. Windows is validated for all items except the full loop above.

macOS has no test. It uses the same code paths as Linux: the POSIX shim, the tmux backend, the
WezTerm backend and `python3`. Thus it is *probably* correct. But Linux was also *probably*
correct before the first true run, and that run found seven defects.

### ✓ Completed Features

- **Phase 1: Framework Architecture** — Config-driven swarm orchestration, role injection, git worktree isolation
- **Phase 2: Cross-Platform Infrastructure** — Windows (PowerShell/Windows Terminal/WezTerm), Unix/macOS (zsh/tmux)
- **Phase 3: Auto-Agent Communication** — SQLite message queues with MCP server, automated role-based message forwarding, full agent chain test passing
- **Phase 4: Channel-Based Messaging** — Replaced SQL inbox polling with a blocking `wait_for_message()` Channel
  - ✓ `kiln-channel` Python MCP server (`kiln/framework/mcp-server/channel.py`) — polls SQLite and blocks until a message arrives, returns it already marked delivered
  - ✓ Per-worktree `.mcp.json` generated with `kiln-db` + `kiln-channel`, correct `KILN_ROLE`/`KILN_BRANCH` env vars injected per agent
  - ✓ Channel debug logs at `.kiln/logs/channel-<role>.log`
  - ✓ `-Stop` flag on `kiln.ps1` to kill orphaned MCP server processes after terminal close
- **Phase 5: Skill-Based Handoff Hardening.** This phase moved the receive and handoff mechanics
  out of the loop templates into two skills. It also corrected the stop failures and the merge
  failures that a live test with more than one cycle found. The test used the LibraryHub example.
  - ✓ The skills `/kiln-receive` and `/kiln-handoff` now do the full receive and send sequence.
    They are in `kiln/project/skills/kiln-receive` and `kiln/project/skills/kiln-handoff`. The
    sequence verifies the INSERT of the handoff and writes it again if necessary.
  - ✓ The "not end-of-turn" rule in the loop templates now also covers the return to
    `/kiln-receive`. Before, it covered only the step that sends the handoff. This corrects a
    confirmed stop: an agent completed a verified handoff and then stopped. It did not wait for
    the next message.
  - ✓ Corrections to `.gitignore` for the paths that Kiln makes again or links: `.kiln`,
    `CLAUDE.md`, `.mcp.json` and `tmp/`. Git recorded these paths accidentally. Thus each
    `/kiln-receive` merge had conflicts.
  - ✓ Kiln now commits `.gitignore` before it makes a worktree. It does this in an existing
    repository also. Thus a new worktree gets the file.
  - ✓ A Claude Code debug log for each agent (`--debug-file`) at
    `.kiln/logs/claude-debug-<role>.log`.
  - ✓ The `kiln-db.ps1` CLI, with the commands `list-messages`, `show-message`, `stats`,
    `retry-message` and `clear-old`. Thus you can examine the message queue and write no SQL.
- **Phase 6: Wrapper and Worker-Subagent Delegation.** This phase made each Claude `auto` role a
  thin wrapper. At each cycle, the wrapper sends its work to a temporary worker subagent. Thus
  the context of the wrapper stays small. It does not collect the full transcript of the work.
  - ✓ Kiln makes the worker agent file `.claude/agents/<role>-worker.md`. The function was
    `Write-GeneratedWorkerAgent` in `kiln.ps1`. It is now `write_worker_file()` in
    `launcher/generate.py`. The file has the role file, `engineering.md` and `project.md`. It
    does not have `workflow.md`, the `Agent` tool or the MCP tools.
  - ✓ The file `loop-auto-claude.md` has a cycle of seven steps: receive, mark, delegate, correct
    a failure, hand off, mark processed, and loop. The state of the message changes at each step.
  - ✓ **A live test verified this phase** through more than 8 cycles of the LibraryHub workflow.
    The result was 50 tests, correct commits, no stops and no lost messages.
- **Phase 6a: Message Lifecycle Tracking** — Full visibility into agent work and recovery from interruptions
  - ✓ `kiln-channel` MCP server adds `mark_processing()` and `mark_processed()` tools for state transitions
  - ✓ Message states: `queued` (created) → `delivered` (retrieved) → `processing` (work started) → `processed` (complete)
  - ✓ `wait_for_message()` checks for both `queued` and `delivered` messages, allowing recovery of unprocessed messages if an agent times out
  - ✓ Full state visibility via `kiln-db.ps1 stats` and database queries

### Current Capabilities

- ✓ Swarms of more than one agent. Two to five agents is usual.
- ✓ A configuration and a role for each agent.
- ✓ Isolated git worktrees with branch names such as `feature/ABC-coder` and `main-refactorer`.
- ✓ Channel messages that block, with a full lifecycle. An agent calls `wait_for_message()`. A
  message goes from `queued` to `delivered` to `processing` to `processed`.
- ✓ Message recovery. If an agent stops after it receives a message, the next agent can take that
  message, because the message is still in the `delivered` state.
- ✓ A receive and handoff sequence with skills (`/kiln-receive` and `/kiln-handoff`). The
  sequence verifies the INSERT of the handoff and writes it again if necessary.
- ✓ A constitution in layers: workflow, engineering and project.
- ✓ Terminal support on all platforms: Windows Terminal, WezTerm and tmux.
- ✓ Flexible terminal layouts: tabs, split panes, grids and focus layouts.
- ✓ A model for each Claude agent.
- ✓ A health check for the communication. Ask `human-in-the-loop` to run the `/kiln-ping` skill.
- ✓ A live dashboard for the full swarm (`"scheduler": "dashboard"`). It shows the state of each
  role, the queue depth, the totals for the cost, the cycles and the tokens, the cache hit rate,
  the recent activity and the escalations, in one pane.
- ✓ Token accounting for each role, from the stream of each backend. Kiln keeps the input, the
  output, the cache read and the cache write separately. It never collects them into one number.
  If a backend reports nothing, Kiln shows nothing. It does not show an incorrect zero.
- ✓ An optional traffic capture proxy (`--proxy`). The path gives the role. Kiln never stores a
  credential. It records metadata only, until you give `--capture full`. It manages a port
  conflict. It has a retention budget. The dashboard has a panel that divides each request into
  the tools, the instructions and the conversation.
- ✓ Two vendors through one proxy. Kiln routes the `claude` roles and the `codex` roles. A live
  test verified both. Each keeps its own subscription authentication. Each role has its own
  upstream. Thus a swarm with more than one backend needs only one proxy.
- ✓ A logbook of each handoff and each action of an agent.
- ✓ Wrapper and worker-subagent delegation for the Claude `auto` roles. A continuous thin wrapper
  sends the work to a temporary worker subagent. Thus the context of the wrapper stays at
  approximately 140 lines through an unlimited quantity of cycles.
- ✓ Support for the Codex agent, and this includes the worker-subagent delegation with the
  multi-agent tools of Codex. Kiln makes `AGENTS.md` and `.codex/agents/<role>-worker.toml`. Each
  role has an isolated `CODEX_HOME` MCP configuration. The start flag is
  `--dangerously-bypass-approvals-and-sandbox`.

### ⚠️ Security Considerations

**Warning: The agents have full permissions by default.** This is necessary for autonomous
operation:

- **Claude agents** use `--permission-mode bypassPermissions`. This approves all MCP tools and
  all file operations automatically.
- **Copilot agents** use `--allow-all`. This approves all GitHub Copilot tools and all file
  access automatically.
- **Codex agents** use `--dangerously-bypass-approvals-and-sandbox`. This approves all tool
  calls and stops the sandbox. This is the equivalent flag of Codex.

  Each Codex role also gets an isolated configuration directory with the variable `CODEX_HOME`.
  The directory is `.kiln/codex-home/<role>/`. Thus Kiln never writes over your true
  `~/.codex/config.toml`.
- **Grok agents** operate in scheduler mode only, because there is no wrapper mode. They use
  `--always-approve`, which approves all tool executions. They also use `--no-subagents`, which
  stops grok from making its own subagents. This is the same isolation as the other three
  backends.

**Warning:** Thus an agent can read, write and execute each file in its worktree, and it asks
for no approval. This is intentional for an autonomous workflow. But you must understand the
risk.

**Traffic capture (`--proxy`):** The proxy operates only when you ask for it. It is local. It
sends data to the vendor only. Kiln never writes the values of the `Authorization` header, an
API-key header, a cookie or an account identifier.

The proxy records **metadata only**: the sizes, the times, the model names and the token counts.
**Kiln records no prompt text without `--capture full`.** That flag is a separate and deliberate
step, for a good reason: the body of a request holds the full source code that the agent read,
as plain text, in a directory that is in each worktree.

**Warning:** Give a `full` `traffic.db` file the same protection as the repository.

**How to decrease the risk:**

- Keep each Kiln project in an isolated directory. Do not use a production directory.
- Do not put sensitive data in the project. This includes credentials, secrets and personal
  data.
- Use the git worktrees for isolation. An agent can only get access to its own worktree and to
  the shared directory `.kiln/`.
- Examine the output and the commits of an agent before you merge them into the main branch.
- For code that you do not trust, or for a condition with a high security requirement, run Kiln
  in a sandbox or a virtual machine.

### Known Limitations and Future Work

This section lists what does *not* operate yet, and what operates with a limitation. For the
items that are validated, refer to the list above.

- **Error handling.** The recovery from an error in an agent workflow is minimal. Kiln does not
  yet decrease its functions in a controlled way.
- **Scaling.** A test with 4 agents over more than 8 cycles showed stable performance. The
  behavior with 10 agents or more is not known.
- **A Copilot scheduler worker is not reliable in a long session.** A non-interactive Copilot CLI
  session (the `-p` flag) of approximately 4 to 8 minutes with many tool calls loses its tool
  approval. There is no indication and no recovery in that session. Each subsequent call that
  writes gets the message `Permission denied and could not request permission from user`.

  A short session with the same flags and the same worktree never has this failure. This appears
  to be a defect in the Copilot CLI. It is not a Kiln configuration problem. The report is
  [github/copilot-cli#4433](https://github.com/github/copilot-cli/issues/4433). The local record
  is [nsd0okernicke/kiln#8](https://github.com/nsd0okernicke/kiln/issues/8).

  Thus Copilot is not in the scheduler-mode rotation of any supplied profile. Copilot is still
  correct for the wrapper-mode (interactive) roles. This failure never occurs there.
- **The traffic capture routes the `claude` roles and the `codex` roles only.** A live test
  verified both, and this includes the subscription authentication. The `grok` CLI can have an
  equivalent override, but this needs a test. The `copilot` CLI communicates with the endpoints
  of GitHub. It probably needs a MITM proxy, which is out of the scope.

  A role on a backend that Kiln does not route operates normally. It is simply absent from the
  capture. The launcher writes a log of the roles that it routes. Thus an empty panel always has
  an explanation.
- **The Copilot token parser has never read a true stream.** Kiln has it from the documented
  event shape of that adapter. No validated run included Copilot. If the shape is different, the
  parser reports nothing. Thus the dashboard shows `-`. It does not show an incorrect number.

  The session store of Copilot is `~/.copilot/session-store.db`. Its table
  `assistant_usage_events` uses the names `input_tokens`, `output_tokens`, `cache_read_tokens`
  and `cache_write_tokens`. Thus the alias table of the parser is probably missing `cache_write`.
  A live capture found the same problem in the Codex parser. One live call would give the answer.
- **`grok` has no wrapper mode.** Its scheduler adapter is real, and a live test verified it. But
  there is no file `loop-auto-grok.md` and no wrapper path. Thus a `grok` role must use `auto`
  mode with `"scheduler": "python"`. It cannot use `manual` mode.
- **Unix parity is real, but nobody tested it until the first true run.** Both shims call the
  same Python file `generate.py`. Thus the template injection, the `auto` and `manual` modes and
  the worker delegation are structurally the same on each platform. Only the terminal backend is
  different.

  That was the theory. But nobody had *run* the Python port on Linux. The first true run
  (Ubuntu 24.04 on WSL2) showed that the theory was largely correct. The full test suite passed
  (932 tests passed and 3 Windows-console tests were skipped). The worktrees, the symlinks, the
  git hooks, the tmux backend and `--stop` all operated.

  But the run also found six defects. Only a true run could find them:
  - The files `bin/*.sh` were not executable (mode `100644`). Thus the documented command
    `./bin/kiln.sh .` stopped with `Permission denied`.
  - The name `python` was in the code for the scheduler, inbox and dashboard panes, and for the
    kiln-channel MCP entry. A standard Debian or Ubuntu system has `python3` only. Thus each of
    these panes stopped immediately with `Command 'python' not found`.
  - The command `kiln init <dir>` is in this document, but argparse refused it.
  - The flag `--terminal none` made PowerShell commands on each platform. Thus the backend whose
    only function is to *show* the command showed Linux users commands that no shell of theirs
    could run.
  - The program `set-status.py` found the project with `KILN_PROJECT_DIR` only. Only the WezTerm
    backend sets that variable. Thus with tmux, the STATE column of the dashboard stayed empty.
  - The MCP install command in this document, and in the error message of Kiln, fails on Debian
    and Ubuntu because of PEP 668.

  All six defects are corrected. **Not yet verified on Linux:** a true swarm cycle against a live
  agent CLI. This needs an authenticated Claude Code in the Linux environment.
- **There is no full-reset script for Unix.** Refer to the known problem in **Cleanup**.
- **Symlinks need Developer Mode on Windows.** Without it, the error is `WinError 1314`. Then a
  worktree *copies* `.kiln` and does not share it. The swarm operates, but the state is not
  truly shared.

### Recommended Next Steps

1. **Test the full scheduler loop with all the backends at the same time.** The all-Claude
   `default` profile completed one continuous cycle:
   human-in-the-loop, specifier, coder, refactorer, architect, human-in-the-loop. Refer to
   **Live validation**.

   Thus two tasks stay. First, do the same run with `mixed-backends`. Second, make more than one
   role compete for the SQLite queue. Each cycle until now was sequential, with one role at a
   time. Thus four schedulers have never competed for the queue.
2. **Make the wrapper mode for `grok`.** This closes the last gap where Kiln accepts a backend
   but one mode is absent. Use the same shape as the Copilot work and the Codex work.
3. **Port `kiln-cleanup.ps1` to Python.** This closes the Unix cleanup gap. It also removes the
   last PowerShell in the start path that is not a shim.
4. **Add error handling.** Kiln must decrease its functions in a controlled way when an agent
   cannot process a message.
5. **Test projects in more languages.** Test more than the LibraryHub FastAPI example.
6. **Add CI/CD integration.** Show how Kiln agents operate in GitHub Actions and GitLab CI.

---

## Acknowledgments

[Uncle Bob's swarm-forge](https://github.com/unclebob/swarm-forge) gave the idea for Kiln.
That project is a framework for development with more than one agent. Kiln uses that design
philosophy. But Kiln gives more attention to TDD workflows, to the MCP message standards, and to
orchestration for AI agents in more than one language and on more than one platform.

---

## Technical Names and Technical Verbs in This Document

ASD-STE100 permits Technical Names and Technical Verbs from the domain of the document. These
are the ones that this document uses. They are not in the general dictionary of the standard.

**Technical Names:**

`agent`, `adapter`, `backend`, `branch`, `cache`, `commit`, `constitution`, `context`,
`credential`, `dashboard`, `endpoint`, `escalation`, `frontmatter`, `git`, `handoff`, `inbox`,
`launcher`, `layout`, `logbook`, `LLM`, `MCP`, `merge`, `model`, `orchestration`, `pane`,
`prompt`, `profile`, `proxy`, `queue`, `repository`, `role`, `scaffolding`, `scheduler`,
`session`, `shim`, `skill`, `subagent`, `swarm`, `tab`, `token`, `topology`, `upstream`,
`worktree`, `wrapper`.

**Product and tool names:** Claude, Claude Code, Codex, Copilot, Grok, Kiln, Python,
PowerShell, SQLite, tmux, WezTerm, Windows Terminal.

**Technical Verbs:** `to commit`, `to merge`, `to squash`, `to escalate`, `to delegate`,
`to route`, `to scaffold`, `to hand off`, `to cache`.
