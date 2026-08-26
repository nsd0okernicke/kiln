<p align="center">
  <img src="docs/images/logo.png" alt="Kiln logo" width="120" />
</p>

# Kiln

Kiln runs local, role-based AI development swarms. Each autonomous role works in its own Git
worktree, receives project-specific instructions, and hands work to the next role through a
shared queue. A human-facing role starts the work, reviews results, and handles escalations.

Kiln is useful when a task benefits from explicit separation of concerns—for example,
specification, implementation, refactoring, and architectural review—and when you want those
steps to be observable and repeatable rather than managed in one long agent conversation.

## What users should know

- Kiln runs on your machine and operates directly on a Git repository.
- Agents can execute commands and modify files without interactive approval.
- Autonomous roles use separate Git worktrees and exchange structured handoffs.
- Profiles define the roles, routing, models, timeouts, and terminal layout.
- Claude, Codex, Copilot, Grok, and Pi are supported as role backends.
- The default profile is human-guided at intake and autonomous afterward.
- Failures are retried, then escalated to the human. Repeated escalation parks the role until
  you explicitly retry it.
- The dashboard and local web cockpit show role state, queues, work items, usage, and failures.

Kiln is an orchestration layer, not an AI provider. You install and authenticate the agent
CLI you intend to use.

## Requirements

- Python 3.11 or newer
- Git
- At least one supported agent CLI on `PATH`:
  - Claude Code
  - OpenAI Codex CLI
  - GitHub Copilot CLI
  - Grok CLI
  - Pi coding agent
- A terminal backend:
  - WezTerm is recommended on every platform
  - Windows Terminal is supported on Windows
  - tmux is supported on Linux and macOS

Wrapper-mode roles also require the MCP Python dependencies:

```bash
python -m pip install -r src/kiln/mcp_server/requirements.txt
```

Kiln warns at launch when the interpreter used by the agent CLI cannot import the required MCP
package.

## Quick start

Clone Kiln and run it in place:

```bash
git clone https://github.com/nsd0okernicke/kiln.git
cd kiln
```

### Windows

Create a project:

```powershell
.\bin\kiln.ps1 -Init -WorkingDir C:\path\to\my-project
```

Launch the default swarm:

```powershell
.\bin\kiln.ps1 -WorkingDir C:\path\to\my-project
```

### Linux and macOS

Create a project:

```bash
./bin/kiln.sh init /path/to/my-project
```

Launch the default swarm:

```bash
./bin/kiln.sh /path/to/my-project
```

Scaffolding initializes Git when necessary and creates the project constitution, roles, skills,
and runtime tools. Launching creates role worktrees, generated agent instructions, the message
queue, and terminal panes.

Before starting a real run, inspect the resolved topology without launching anything:

```bash
./bin/kiln.sh /path/to/my-project --dry-run
```

Use `.\bin\kiln.ps1` instead of `./bin/kiln.sh` in the remaining examples on Windows.

## The default workflow

The `full` profile runs this cycle:

![Kiln's default role topology, from human intake through the autonomous development cycle](docs/images/agentic_coding_topology_human_left_v3.svg)

The human-facing role gathers and confirms the request. The autonomous roles then:

1. turn the request into an implementable specification;
2. implement it;
3. improve tests, coverage, design, and mutation resistance;
4. review the result and either return it to the human or start another lap.

An inbox pane displays human-directed messages and escalations. The terminal dashboard and web
cockpit provide a swarm-wide view.

## Choosing a profile

Profiles describe the kind of work, independently of the AI backend.

| Profile | Use it for |
|---|---|
| `full` | New features that benefit from specification, implementation, hardening, and review |
| `fix` | Bugs and smaller changes; coder followed by architect review |
| `spike` | Short-lived exploration where the output is knowledge rather than production code |
| `harden` | Existing code that needs stronger tests, coverage, mutation resistance, or boundaries |
| `dry-run` | Learning the workflow with manual roles and human approval at each step |

List profiles:

```bash
./bin/kiln.sh --list-profiles
```

Launch a profile:

```bash
./bin/kiln.sh /path/to/project --profile fix
```

The shipped workflow profiles use Pi by default: HITL runs `igate/brain`, while automated
roles run `igate/coder`. Configure those models in Pi before launching, or select another
backend with an override.

Run every agent-bearing role on another backend:

```bash
./bin/kiln.sh /path/to/project --profile harden --agent-override codex
```

Backend model names are not portable. An agent override therefore clears profile models unless
you explicitly provide one:

```bash
./bin/kiln.sh /path/to/project \
  --agent-override codex \
  --model-override gpt-5-codex
```

Pi uses custom OpenAI-compatible providers configured in Pi's user-owned `models.json` and
`settings.json`. Kiln references only provider-qualified model names:

```bash
./bin/kiln.sh /path/to/project --agent-override pi --model-override igate/coder
```

Keep provider URLs, API keys, tokens, and corporate certificate configuration in Pi's user
configuration or environment. Kiln does not copy them into profiles, worktrees, generated
worker definitions, commands, or logs. Scheduler workers run Pi with `--mode json`, an
ephemeral session, project-local configuration disabled, and an explicit built-in tool list.

On corporate Windows networks, Pi may report `Connection error` when Node does not trust the
company certificate authority. Keep the provider URL on HTTPS and make Node use the Windows
certificate store, then reopen the terminal before launching Kiln:

```powershell
[Environment]::SetEnvironmentVariable("NODE_OPTIONS", "--use-system-ca", "User")
```

## Daily commands

### Launch

```bash
./bin/kiln.sh /path/to/project [--profile NAME]
```

Useful options:

| Option | Purpose |
|---|---|
| `--dry-run` | Print resolved commands without launching |
| `--terminal wezterm\|wt\|tmux\|none` | Select or disable terminal launching |
| `--agent-override BACKEND` | Replace every agent backend in the selected profile |
| `--model-override MODEL` | Model used with an agent override |
| `--proxy` | Enable local metadata capture for Claude and Codex traffic |
| `--capture full` | Also retain request and response bodies |
| `--verbose` | Enable detailed launcher logging |

Run `./bin/kiln.sh --help` for the complete list. PowerShell aliases such as `-WorkingDir` and
`-ProfileName` are accepted by the same Python CLI.

### Send work

Create work in the human backlog while the swarm is running:

```bash
./bin/kiln.sh task create cat-2 --title "Search by author" --body "Add author search to the catalog"
./bin/kiln.sh task update cat-2 --body "Add author search, including partial-name matching"
./bin/kiln.sh task list
./bin/kiln.sh task show cat-2
./bin/kiln.sh task handoff cat-2
./bin/kiln.sh task archive cat-3
```

Tasks remain editable and consume no agent time until handoff. The Cockpit provides the same
create, edit, handoff, and archive operations. Use `kiln task --help` for filters and for
targeting a role other than the configured intake role.

To bypass the backlog and queue a direct intervention:

```bash
./bin/kiln.sh send "add pagination to GET /books" --to specifier
```

The launcher resolves the active project, branch, and message database. Use `--working-dir`
when running the command outside the project directory.

### Inspect and retry failures

List failed work:

```bash
./bin/kiln.sh retry
```

Retry an item with additional guidance:

```bash
./bin/kiln.sh retry 4f3a91c2 --guidance "the fixtures are beside the unit tests"
```

Retrying preserves the original work item, history, failure reason, and accumulated metrics.

### Stop

```bash
./bin/kiln.sh /path/to/project --stop
```

On Windows:

```powershell
.\bin\kiln.ps1 -WorkingDir C:\path\to\project -Stop
```

## Execution model

Kiln supports two agent execution modes.

### Scheduler mode

Autonomous roles normally use the deterministic Python scheduler. For each message it:

1. claims the message from SQLite;
2. merges the sender's commit;
3. invokes a one-shot worker with the role and project instructions;
4. runs the configured verification command, if any;
5. commits the role's changes;
6. sends a structured handoff to the next role.

The scheduler owns retries, timeouts, cost and cycle limits, status reporting, and escalation.
Workers cannot access the handoff queue directly.

<details>
<summary>See one scheduler cycle</summary>

![A scheduler cycle from polling through worker execution, verification, handoff, and escalation](docs/images/diagram-scheduler-cycle.svg)

</details>

### Wrapper mode

Manual roles use a persistent interactive agent session. The wrapper reads generated project
instructions and uses Kiln skills and MCP tools to receive and send work. Wrapper mode is most
appropriate for the human-facing role, where a continuing conversation is useful.

<details>
<summary>See the wrapper and delegated-worker cycle</summary>

![A persistent wrapper receiving work and delegating it to a disposable worker](docs/images/diagram-coder-internal-cycle.svg)

</details>

## Git and project isolation

Kiln creates one worktree for each autonomous role under `.worktrees/`. The main project
directory is normally used by `human-in-the-loop`.

Each role receives incoming work through Git and produces a role-prefixed squash commit. Kiln
records provenance so later laps retain ancestry even though handoffs are squashed. Append-only
`logbook.md` files use Git's union merge driver.

Runtime files are stored under `.kiln/` and generated worktree files are ignored. Do not commit
`.kiln/`, `.worktrees/`, generated agent instructions, or per-role MCP configuration.

## Project files

After initialization, the important user-owned files are:

```text
my-project/
├── README.md                         # project brief
├── kiln.profiles.json                # optional project-specific profiles
└── kiln/
    └── project/
        ├── constitution.md            # instruction loading order
        ├── constitution/              # project and engineering rules
        ├── roles/                     # role responsibilities
        └── skills/                    # reusable agent workflows
```

Generated runtime state includes:

```text
.kiln/
├── messages.db                        # task backlog, handoff queue, and history
├── status/                            # current role state
├── logs/                              # scheduler and optional agent diagnostics
├── sessions                           # launched role inventory
├── traffic.db                         # present when proxy capture is used
└── cockpit.url                        # current local cockpit URL
```

Customize files under `kiln/project/`; Kiln copies them into role worktrees at launch.

### Adapt the project constitution

Initialization provides safe generic constitution files. Before using Kiln on a new codebase,
ask the interactive HITL agent to use `kiln-constitution-setup`. The skill inspects existing
manifests, source layout, tests, CI, and documentation first, then asks only about decisions it
cannot establish. For a new project it uses a guided interview instead.

The skill proposes complete replacements for `engineering.md` and `project.md` and waits for
review before overwriting either file. It does not change workflow, roles, profiles, or routing.

## Profile configuration

Create `kiln.profiles.json` in the project root to replace the bundled profile set. Profile
files are JSON and are not merged with framework defaults, so copy
`src/kiln/resources/profiles.json` when you want to modify an existing profile.

Minimal example:

```json
{
  "default": "fix",
  "profiles": {
    "fix": {
      "description": "Coder followed by architecture review",
      "defaults": {
        "agent": "pi",
        "model": "igate/coder"
      },
      "terminals": [
        {
          "role": "human-in-the-loop",
          "worktree": "@current",
          "mode": "manual",
          "model": "igate/brain"
        },
        {
          "role": "coder",
          "worktree": "coder",
          "mode": "auto",
          "scheduler": "python",
          "workerTimeout": 1800
        },
        {
          "role": "architect",
          "worktree": "architect",
          "mode": "auto",
          "scheduler": "python",
          "workerTimeout": 2400
        }
      ],
      "routing": {
        "human-in-the-loop": "coder",
        "coder": "architect",
        "architect": "human-in-the-loop"
      }
    }
  }
}
```

Each handing-off role must have a valid routing target. Unknown keys, duplicate roles, missing
targets, and unsupported backend names fail at launch instead of being silently ignored.

### Role fields

| Field | Meaning |
|---|---|
| `role` | Stable role name used for routing, worktrees, status, and logs |
| `agent` | `claude`, `codex`, `copilot`, `grok`, or `pi` |
| `title` | Optional display title |
| `model` | Model for the wrapper or one-shot worker |
| `workerModel` | Optional model specifically for delegated worker execution |
| `worktree` | `@current` or a name below `.worktrees/` |
| `mode` | `manual` or `auto` |
| `scheduler` | `python`, `inbox`, `dashboard`, or `cockpit`; omit for wrapper mode |
| `watches` | Role queue monitored by an inbox pane |
| `workerDebug` | Retain additional worker diagnostics |
| `verify` | Shell command that must pass before handoff |
| `verifyTimeout` | Maximum duration of verification |
| `pollInterval` | Scheduler polling interval |
| `workerTimeout` | Maximum duration of a worker invocation |
| `workerIdleTimeout` | Maximum silence before the worker is terminated; `0` disables it |
| `maxAttempts` | Worker attempts before escalation |
| `maxCycles` | Maximum visits by one work item to this role |
| `maxBudgetUsd` | Per-work-item cost cap where the backend reports cost |
| `escalationLimit` | Consecutive escalations before the role parks |
| `activityLimit` | Maximum scheduler activity before escalation |
| `bell` | Ring the terminal bell when an inbox receives work |
| `port` | Preferred cockpit port |
| `openBrowser` | Open the cockpit in a browser at launch |

Profiles may define `defaults` for repeated terminal fields. Values on a terminal entry override
the defaults.

## Dashboard and cockpit

The terminal dashboard shows:

- role state and elapsed time;
- queue depth and oldest wait;
- current work item and attempt;
- cycle, token, cache, and cost totals;
- recent handoffs and escalations;
- optional traffic-capture statistics.

The cockpit exposes the same state as a local web interface and adds actions for managing the
human-owned backlog in the HITL lane, sending work, retrying, stopping roles, and tearing down
the swarm. Backlog tasks can be created, edited, handed off, or archived there. The cockpit
binds only to `127.0.0.1` and probes upward from its preferred port when necessary. The active
URL is written to `.kiln/cockpit.url`.

![Kiln Cockpit showing the role board, active work, queue controls, usage, and recent activity](docs/images/cockpit.png)

### Test health

The cockpit shows a **Test health** panel when a project describes where its test reports
live. Create `.kiln/test-metrics.json`:

```json
{
  "framework": "pytest",
  "command": "python -m pytest --junitxml={reports}/junit.xml --cov=yourpackage --cov-branch --cov-report=xml:{reports}/coverage.xml",
  "verificationRole": "architect",
  "reports": {
    "junit": "reports/junit.xml",
    "coverage": "reports/coverage.xml",
    "lint": "reports/ruff.sarif"
  },
  "maxAgeMinutes": 30
}
```

`verificationRole` makes that scheduler role run `command` after its worker succeeds and
before handoff. Use `{reports}` in the command when reports must be written to the shared
project rather than the role's worktree; Kiln creates and expands that directory portably.
The command itself runs in the verification role's worktree, so project-relative tools should
use `.` rather than `{project}`. Kiln keeps the generated `reports/` directory out of Git.

Three **formats** are read, never three tools — which is what keeps the panel working in any
ecosystem:

| Key | Format | Written natively by |
| --- | --- | --- |
| `junit` | JUnit XML | pytest `--junitxml`, Maven, Gradle, Jest, Vitest, `dotnet test` |
| `coverage` | Cobertura XML | coverage.py `--cov-report=xml`, JaCoCo, Istanbul |
| `lint` | SARIF 2.1.0 | ruff, ESLint, PMD, SpotBugs, CodeQL |

"JUnit" and "Cobertura" name file formats, not the Java tools they came from; `--junitxml` is
a built-in pytest flag and coverage.py's XML is Cobertura by its own DTD reference. The same
config in a Java project differs only in paths:

```json
{
  "framework": "gradle",
  "command": "./gradlew test jacocoTestReport pmdMain",
  "reports": {
    "junit": "build/test-results/test",
    "coverage": "build/reports/jacoco/test/jacocoTestReport.xml",
    "lint": "build/reports/pmd"
  }
}
```

The nominated `verificationRole` runs `command` before handoff; the Cockpit only reads the
resulting reports. Relative report paths resolve from the project root and may name a file or
directory. Results older than `maxAgeMinutes` are shown as stale, while missing or malformed
reports are reported without affecting the rest of the Cockpit.

`.kiln/test-metrics.json` is local runtime configuration. Add it manually for custom projects;
supported examples may provide it during initialization.

## Traffic capture

Traffic capture is off by default. Enable metadata capture with:

```bash
./bin/kiln.sh /path/to/project --proxy
```

Metadata mode records timing, sizes, model names, token usage, and per-request composition.
It does not retain prompt or response text.

Full capture is a separate opt-in:

```bash
./bin/kiln.sh /path/to/project --proxy --capture full
```

Full request bodies can contain source code, prompts, and model output. Treat `.kiln/traffic.db`
as sensitive. Credential, cookie, and stable account/session identifier values are redacted
before storage. Capture currently routes Claude and Codex roles; other backends continue
normally but do not appear in traffic statistics.

## Safety

Kiln is designed for autonomous execution and launches agents with broad permissions:

- Claude workers use bypassed permission prompts.
- Codex workers bypass approvals and the sandbox.
- Copilot workers allow tool execution and file access.
- Grok scheduler workers auto-approve tools.
- Pi scheduler workers use an ephemeral JSON-mode session and ignore project-local Pi
  configuration; Pi's user-owned provider credentials remain in effect.

Agents can run arbitrary commands available to your user account. Git worktrees separate role
branches, but they are not a security sandbox and do not prevent access outside the repository.

Recommended precautions:

- use a disposable or dedicated development directory;
- keep secrets and production credentials out of the project;
- review commits before pushing or merging them;
- run untrusted projects inside a VM or container;
- start required external services, such as a container engine, before launching agents that
  depend on them.

## Troubleshooting

### Nothing launches

Run a dry launch and verbose diagnostics:

```bash
./bin/kiln.sh /path/to/project --dry-run --verbose
```

Confirm Python, Git, the configured agent CLI, and the selected terminal are on `PATH`.

### A wrapper cannot receive messages

Install the MCP requirements into the interpreter resolved by the agent CLI. Kiln prints the
interpreter and install command when its startup probe fails.

### A role is blocked or halted

Inspect the dashboard, `.kiln/logs/`, and the failed-message list:

```bash
./bin/kiln.sh retry
```

Then retry with guidance. A halted scheduler remains alive specifically so it can receive that
retry.

### A worker appears stuck

Workers have total and idle timeouts. When a timeout fires, Kiln terminates the worker process
tree, records the failure, and follows the normal retry/escalation policy. Increase
`workerTimeout` only when the role's legitimate toolchain needs more time.

### Worktrees conflict

Kiln warns about stale worktrees before launch and automatically handles its own generated
files. Conflicts in project files still require ordinary Git resolution. Stop the swarm before
manual worktree surgery.

### Status or cockpit data looks stale

Check `.kiln/status/`, `.kiln/sessions`, and `.kiln/cockpit.url`. Restarting Kiln repairs
generated configuration and recovers messages left in `processing` by an interrupted cycle.

## Development

Install development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the test and quality gates:

```bash
python -m pytest
python -m pytest tests/acceptance
ruff format --check src tests tools
ruff check src tests tools
pyright
python tools/quality_metrics.py --tier deterministic
```

The implementation lives under `src/kiln/` and follows domain/application/infrastructure
boundaries. Tests are split into unit, property, integration, acceptance, and opt-in live tiers.
The acceptance suite is separate from the default run and uses local fake workers; it needs no
agent credentials. Add a system scenario for a public workflow or regression that crosses
process, Git, database, filesystem, or HTTP boundaries and cannot be protected by one adapter's
integration tests alone.

## Current limitations

- Kiln has been exercised primarily with small swarms; behavior at substantially larger role
  counts is not established.
- Copilot non-interactive workers can lose tool approval during long sessions due to an
  upstream CLI issue. Avoid Copilot for long scheduler-mode work until that behavior is fixed.
- Traffic capture supports Claude and Codex only.
- Grok wrapper mode is less extensively validated than its scheduler adapter.
- Pi integration is covered with a fake CLI. A real private provider should pass the issue 32
  compatibility checks for tools, streaming, usage, termination, certificates, and proxies
  before production use.
- Linux/macOS share the Python core with Windows, but authenticated live-agent coverage varies
  by backend and environment.
- Windows symlink creation may require Developer Mode or elevation. Kiln falls back to copies,
  which reduces sharing between worktrees.

## License and status

Kiln is under active development. Treat profile formats and backend integrations as evolving
interfaces, pin a known working revision for important projects, and review release changes
before upgrading an active swarm.
