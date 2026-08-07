"""
The command injected into each terminal pane.

The PowerShell original built these as pre-quoted strings, separately in
`Build-WezTermAgentCommand` and `Get-WindowsTerminalAgentCommand`, with `kiln.sh` carrying a
third copy for tmux — three places that had to agree on both the flags and the quoting, and
periodically did not.

Here a command is built once as structured data (`AgentCommand`: argv + env + banner) and
rendered per host shell. Flags live in one place; quoting lives in the renderers.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .config import RoleConfig
from .paths import KilnPaths

#: Opening prompt handed to an interactive wrapper session.
START_PROMPT = "Start your role session."

#: Fallback when a Claude role omits `model` in its profile entry.
DEFAULT_CLAUDE_MODEL = "sonnet"


@dataclass(frozen=True)
class AgentCommand:
    """A pane's command, independent of which shell will host it."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    #: Printed before the agent starts. Copilot gets one because its CLI shows no role name.
    banner: str = ""

    def with_env(self, **extra: str) -> AgentCommand:
        return AgentCommand(argv=self.argv, env={**self.env, **extra}, banner=self.banner)


def _claude_command(role: RoleConfig, paths: KilnPaths) -> AgentCommand:
    permission_mode = "default" if role.mode == "manual" else "bypassPermissions"
    argv = [
        "claude",
        "--model", role.model or DEFAULT_CLAUDE_MODEL,
        "--permission-mode", permission_mode,
        "--mcp-config", "./.mcp.json",
        "--debug-file", str(paths.agent_debug_log(role.role)),
    ]
    if role.display_name:
        argv += ["-n", role.display_name]
    argv.append(START_PROMPT)
    return AgentCommand(argv=argv)


def _copilot_command(role: RoleConfig) -> AgentCommand:
    argv = ["copilot", "--allow-all"]
    if role.model:
        argv += ["--model", role.model]
    if role.display_name:
        argv += ["--name", role.display_name]
    argv += ["-i", START_PROMPT]
    return AgentCommand(argv=argv, banner=role.display_name)


def _codex_command(role: RoleConfig, paths: KilnPaths) -> AgentCommand:
    # CODEX_HOME relocates Codex's whole config dir, so each role gets isolated trust and
    # MCP settings without touching the user's real ~/.codex/config.toml.
    return AgentCommand(
        argv=["codex", "--dangerously-bypass-approvals-and-sandbox", START_PROMPT],
        env={"CODEX_HOME": str(paths.codex_home(role.role))},
    )


def _scheduler_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Launch the deterministic scheduler instead of an LLM wrapper session.

    Invoked as `python -m scheduler.role_scheduler`, NOT as a bare script path: the package
    uses relative imports, so running the file directly fails with "attempted relative
    import with no known parent package". PYTHONPATH points at kiln/framework so both
    `scheduler` and `launcher` resolve.
    """
    argv = [
        "python", "-m", "scheduler.role_scheduler",
        "--role", role.role,
        "--branch", branch,
        "--db-path", str(paths.db_path),
        "--worktree", str(_worktree_for(role, paths)),
        "--workflow", str(paths.workflow_md),
        "--worker-agent", str(paths.worker_agent_file(role.role, role.agent)),
        "--agent", role.agent,
    ]
    model = role.worker_model or role.model
    if model:
        argv += ["--model", model]

    status_script = paths.state_tools_dir / "set-status.py"
    argv += ["--status-script", str(status_script)]
    # A pane's scrollback disappears with the window; a crashed scheduler must still be
    # diagnosable afterwards.
    argv += ["--log-file", str(paths.scheduler_log(role.role))]

    return AgentCommand(
        argv=argv,
        env={
            "PYTHONPATH": str(paths.python_package_root),
            # Python's stdout defaults to the console codepage on Windows (cp1252 here), and
            # printing a non-Latin-1 character then raises UnicodeEncodeError and kills the
            # scheduler mid-cycle. The narration uses emoji, so force UTF-8.
            "PYTHONIOENCODING": "utf-8",
        },
        banner=f"{role.display_name} (scheduler)",
    )


def _worktree_for(role: RoleConfig, paths: KilnPaths) -> Path:
    return paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)


def build_agent_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Build the pane command for one role.

    Scheduler-enabled roles bypass the agent CLI entirely — the scheduler invokes the worker
    itself, one shot per handoff.
    """
    if role.uses_scheduler:
        return _scheduler_command(role, paths, branch)

    if role.agent == "claude":
        return _claude_command(role, paths)
    if role.agent == "copilot":
        return _copilot_command(role)
    if role.agent == "codex":
        return _codex_command(role, paths)

    # `grok` is configurable but has no launch implementation; say so in the pane rather
    # than failing the whole swarm launch.
    return AgentCommand(argv=["echo", f"Agent {role.agent} is not supported yet"])


# --- rendering ---------------------------------------------------------------------

def _quote_powershell(value: str) -> str:
    """Single-quoted PowerShell literal; embedded quotes double."""
    return "'" + value.replace("'", "''") + "'"


def render_powershell(command: AgentCommand) -> str:
    """
    Render for a `pwsh` pane (WezTerm send_text, or `wt.exe ... pwsh -NoExit -Command`).

    The executable is invoked through `&` so a quoted path with spaces still runs.
    """
    parts: list[str] = []
    for name, value in command.env.items():
        parts.append(f"$env:{name} = {_quote_powershell(value)}")
    if command.banner:
        parts.append(f"Write-Host {_quote_powershell(command.banner)} -ForegroundColor Cyan")

    program, *arguments = command.argv
    quoted = [f"& {_quote_powershell(program)}"]
    quoted += [_quote_powershell(argument) for argument in arguments]
    parts.append(" ".join(quoted))
    return "; ".join(parts)


def render_posix(command: AgentCommand) -> str:
    """Render for an sh/zsh pane (tmux send-keys)."""
    parts: list[str] = []
    for name, value in command.env.items():
        parts.append(f"export {name}={shlex.quote(value)}")
    if command.banner:
        parts.append(f"echo {shlex.quote(command.banner)}")
    parts.append(shlex.join(command.argv))
    return "; ".join(parts)
