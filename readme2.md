<p align="center">
  <img src="docs/images/logo.png" alt="Kiln logo" width="120" />
</p>

# Kiln

Kiln runs a team of AI coding agents on your project. Each role works in its own Git
worktree, passes work through a shared queue, and reports progress in a terminal dashboard.
You stay in control through a human-facing agent that accepts requests and returns results.

Use Kiln when you want a repeatable workflow—specification, implementation, refactoring, and
review—instead of one agent trying to do everything in a single conversation.

![Kiln running in WezTerm](docs/images/kiln1.png)

## Before you start

You need:

- Python 3.11+
- Git
- WezTerm (recommended), Windows Terminal, or tmux
- At least one authenticated agent CLI: Claude, Codex, Copilot, or Grok

Kiln is cloned and run in place; it is not installed as a package. Its agents run with broad
file and command permissions so they can work autonomously. Use a non-production repository,
keep secrets out of it, and review the resulting commits before merging.

## Quick start

Clone Kiln, then scaffold a project. Keep the Kiln clone and your project as separate paths.

### Windows (PowerShell)

```powershell
git clone https://github.com/nsd0okernicke/kiln.git C:\tools\kiln

# Create a project
C:\tools\kiln\bin\kiln.ps1 -Init -WorkingDir C:\projects\my-project

# Launch Kiln for it
C:\tools\kiln\bin\kiln.ps1 -WorkingDir C:\projects\my-project
```

### Linux and macOS

```bash
git clone https://github.com/nsd0okernicke/kiln.git ~/tools/kiln

# Create a project
~/tools/kiln/bin/kiln.sh init ~/projects/my-project

# Launch Kiln for it
~/tools/kiln/bin/kiln.sh ~/projects/my-project
```

On first launch, Kiln checks its Python dependencies and prints the appropriate install
command if anything is missing.

Kiln creates role-specific worktrees, opens the configured terminal layout, and starts the
default workflow. Enter your request in the **Human-in-the-Loop** pane; completed work returns
there for your review.

Want a ready-made demo? Add `-Example library-hub` on Windows or
`--example library-hub` on Linux/macOS when creating the project. Other included examples are
`library-hub-java` and `battlezone`.

## Choose a workflow

Profiles describe which roles participate in a job:

| Profile | Best for |
|---|---|
| `full` | New features: specify, code, refactor, and review |
| `fix` | Bugs and small changes |
| `spike` | Fast, throwaway exploration |
| `harden` | Improving and reviewing existing code |
| `dry-run` | Learning the workflow with manual approval at every step |

List the available profiles:

```powershell
C:\tools\kiln\bin\kiln.ps1 -ListProfiles
```

Launch a different profile:

```powershell
C:\tools\kiln\bin\kiln.ps1 -WorkingDir C:\projects\my-project -Profile fix
```

The Unix equivalents use `~/tools/kiln/bin/kiln.sh`, `--list-profiles`,
`--profile fix`, and the project path.

## Everyday commands

| Task | Windows flag | Unix/macOS flag |
|---|---|---|
| Preview without launching | `--dry-run` | `--dry-run` |
| Select a terminal | `-Terminal wezterm` | `--terminal wezterm` |
| Use one agent backend for all roles | `-AgentOverride codex` | `--agent-override codex` |
| Stop the swarm | `-Stop` | `--stop` |
| Show detailed startup logs | `-Debug` | `--verbose` |

Kiln auto-detects a terminal when none is specified. WezTerm provides the richest layout and
live status display; Windows Terminal and tmux are simpler fallbacks.

## Customize your project

After scaffolding, edit:

- `kiln/project/constitution/project.md` for the stack, architecture, and project rules
- `kiln/project/constitution/engineering.md` for quality standards
- `kiln/project/roles/` to change what each agent is responsible for
- `kiln.profiles.json` at the project root to replace the bundled workflow profiles

Runtime state lives in `.kiln/`; agent worktrees live in `.worktrees/`. Both are generated and
Git-ignored. Your project configuration under `kiln/project/` is version-controlled.

## Troubleshooting

- Run with `--dry-run` to inspect the resolved roles, commands, and working directories.
- Run with `--verbose` (`-Debug` on Windows) for startup diagnostics.
- Check `.kiln/logs/` when an agent stalls or exits.
- Use `--stop` before relaunching if a previous swarm did not shut down cleanly.
- On Windows, enable Developer Mode so Kiln can create the symlinks used to share runtime
  state between worktrees.

For implementation details, advanced profiles, traffic capture, queue commands, and current
limitations, see the full [README](README.md).

## Acknowledgments

Kiln was inspired by [swarm-forge](https://github.com/unclebob/swarm-forge).
