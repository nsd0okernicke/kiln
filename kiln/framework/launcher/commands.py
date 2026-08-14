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

from scheduler.adapters import codex_adapter

from .config import RoleConfig
from .paths import KilnPaths, python_command

#: Opening prompt handed to an interactive wrapper session.
START_PROMPT = "Start your role session."

#: Fallback when a Claude role omits `model` in its profile entry.
DEFAULT_CLAUDE_MODEL = "sonnet"

#: Backends whose traffic can be routed through the local proxy.
#:
#: Both entries are *verified live*, not assumed: each CLI honours the override and still
#: attaches its own subscription credential to a local, non-vendor host — Claude via OAuth,
#: Codex via its ChatGPT token. `grok` is unspiked and `copilot` talks to GitHub's endpoints
#: with no override anyone has found, so routing either would be a guess that silently does
#: nothing or breaks their auth.
PROXY_CAPABLE_AGENTS = frozenset({"claude", "codex"})

#: Env var each proxy-capable backend reads for its API base URL.
#:
#: Codex is the odd one: it has no base-URL variable of its own and needs `-c` overrides on
#: the command line instead. Kiln carries the URL in its own variable and each Codex call
#: translates it (`codex_adapter.proxy_config_args`), so the transport stays uniform —
#: pane environment, inherited by the one-shot worker — while the CLI-specific spelling
#: lives with the adapter.
PROXY_BASE_URL_VARS = {
    "claude": "ANTHROPIC_BASE_URL",
    "codex": codex_adapter.PROXY_BASE_URL_ENV,
}

#: Upstream each routed backend's traffic must actually reach, as `host[/base-path]`.
#:
#: The proxy defaults to Anthropic, so only the exception is listed. Codex sends
#: `POST /responses` relative to its base URL; upstream that has to become
#: `/backend-api/codex/responses`, which is what the base path supplies.
PROXY_UPSTREAMS = {"codex": "chatgpt.com/backend-api/codex"}


def proxy_env(role: RoleConfig, proxy_url: str | None) -> dict[str, str]:
    """
    The base-URL override for one role, or nothing.

    The URL carries the role name as a path prefix (`/kiln/<role>`) because a proxy sees
    HTTP requests, not roles — without it the capture is an undifferentiated blob that
    cannot answer "what did the refactorer cost". `proxy.server.split_role` strips the
    prefix again before forwarding.

    Returns `{}` for a backend with no verified override, so enabling the proxy on a mixed
    profile routes what it can and leaves the rest untouched rather than failing the launch.
    """
    if not proxy_url or role.agent not in PROXY_CAPABLE_AGENTS:
        return {}
    variable = PROXY_BASE_URL_VARS[role.agent]
    return {variable: f"{proxy_url.rstrip('/')}/kiln/{role.role}"}


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


def _codex_command(
    role: RoleConfig, paths: KilnPaths, proxy_url: str | None = None
) -> AgentCommand:
    # CODEX_HOME relocates Codex's whole config dir, so each role gets isolated trust and
    # MCP settings without touching the user's real ~/.codex/config.toml.
    #
    # The proxy overrides go on the argv rather than in that config, because the same flags
    # then read identically here and in the one-shot worker call, which cannot use a config
    # file at all (it passes --ignore-user-config).
    argv = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    argv += codex_adapter.proxy_config_args(_proxy_base_url(role, proxy_url))
    argv.append(START_PROMPT)
    return AgentCommand(
        argv=argv,
        env={"CODEX_HOME": str(paths.codex_home(role.role))},
    )


def _proxy_base_url(role: RoleConfig, proxy_url: str | None) -> str | None:
    """The role's proxy URL, or None when this role is not routed."""
    return proxy_env(role, proxy_url).get(PROXY_BASE_URL_VARS.get(role.agent, ""))


def _scheduler_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Launch the deterministic scheduler instead of an LLM wrapper session.

    Invoked as `python -m scheduler.role_scheduler`, NOT as a bare script path: the package
    uses relative imports, so running the file directly fails with "attempted relative
    import with no known parent package". PYTHONPATH points at kiln/framework so both
    `scheduler` and `launcher` resolve.
    """
    argv = [
        python_command(), "-m", "scheduler.role_scheduler",
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
    if role.worker_debug:
        argv += ["--worker-debug"]

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


def _inbox_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Launch the human's notification pane.

    Watches another role's queue rather than its own: the pane is called `inbox`, but the
    messages it shows are addressed to `human-in-the-loop`.
    """
    argv = [
        python_command(), "-m", "scheduler.inbox",
        "--role", role.watched_role,
        "--branch", branch,
        "--db-path", str(paths.db_path),
        # The human is a real role in the graph, not just a notification target: an inbound
        # handoff must be merged into their tree or the work they are asked to review is not
        # actually there. This is /kiln-receive steps 1 and 4, done deterministically.
        "--worktree", str(_worktree_for(role, paths)),
        "--log-file", str(paths.scheduler_log(role.role)),
    ]
    return AgentCommand(
        argv=argv,
        env={
            "PYTHONPATH": str(paths.python_package_root),
            "PYTHONIOENCODING": "utf-8",
        },
    )


def _dashboard_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Launch the swarm-wide dashboard pane.

    Aggregates every role in the profile rather than watching one, so unlike `_inbox_command`
    it needs no `--role`/`--worktree` for someone else's queue -- just the shared DB, status
    directory, and the role inventory `workspace.write_sessions_file` already wrote.
    """
    argv = [
        python_command(), "-m", "scheduler.dashboard",
        "--branch", branch,
        "--db-path", str(paths.db_path),
        "--status-dir", str(paths.status_dir),
        "--sessions-file", str(paths.sessions_file),
        "--project-name", paths.project_root.name,
        "--log-file", str(paths.scheduler_log(role.role)),
        # Always passed, never conditional on the proxy being enabled: the dashboard hides
        # the panel when the store is absent, so one code path covers both cases and a
        # swarm launched without the proxy is not a different dashboard.
        "--traffic-db", str(paths.traffic_db),
    ]
    return AgentCommand(
        argv=argv,
        env={
            "PYTHONPATH": str(paths.python_package_root),
            "PYTHONIOENCODING": "utf-8",
        },
    )


def _worktree_for(role: RoleConfig, paths: KilnPaths) -> Path:
    return paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)


def build_agent_command(
    role: RoleConfig, paths: KilnPaths, branch: str, proxy_url: str | None = None
) -> AgentCommand:
    """
    Build the pane command for one role.

    Scheduler-enabled roles bypass the agent CLI entirely — the scheduler invokes the worker
    itself, one shot per handoff.

    `proxy_url` routes this role's API traffic through the local capture proxy. It is applied
    to both scheduler and wrapper roles: a scheduler-mode worker is a subprocess of its pane
    and inherits the pane's environment, so setting it once on the pane covers both.
    """
    if role.is_inbox:
        return _inbox_command(role, paths, branch)

    if role.is_dashboard:
        return _dashboard_command(role, paths, branch)

    if role.uses_scheduler:
        return _scheduler_command(role, paths, branch).with_env(**proxy_env(role, proxy_url))

    if role.agent == "claude":
        return _claude_command(role, paths).with_env(**proxy_env(role, proxy_url))
    if role.agent == "copilot":
        return _copilot_command(role)
    if role.agent == "codex":
        return _codex_command(role, paths, proxy_url).with_env(**proxy_env(role, proxy_url))

    # `grok` is configurable but has no launch implementation; say so in the pane rather
    # than failing the whole swarm launch.
    return AgentCommand(argv=["echo", f"Agent {role.agent} is not supported yet"])


# --- rendering ---------------------------------------------------------------------

def _quote_powershell(value: str) -> str:
    """Single-quoted PowerShell literal; embedded quotes double."""
    return "'" + value.replace("'", "''") + "'"


def render_powershell(command: AgentCommand, clear: bool = False) -> str:
    """
    Render for a `pwsh` pane (WezTerm send_text, or `wt.exe ... pwsh -NoExit -Command`).

    The executable is invoked through `&` so a quoted path with spaces still runs.

    `clear` is for hosts that *type* this command into a live prompt — WezTerm's
    `send_text` and tmux's `send-keys`. The shell echoes what it was given, so the pane
    opens on a wall of quoted flags before anything useful appears. Clearing as the first
    statement wipes that echo, leaving the agent's own banner at the top.
    """
    parts: list[str] = ["Clear-Host"] if clear else []
    for name, value in command.env.items():
        parts.append(f"$env:{name} = {_quote_powershell(value)}")
    if command.banner:
        parts.append(f"Write-Host {_quote_powershell(command.banner)} -ForegroundColor Cyan")

    program, *arguments = command.argv
    quoted = [f"& {_quote_powershell(program)}"]
    quoted += [_quote_powershell(argument) for argument in arguments]
    parts.append(" ".join(quoted))
    return "; ".join(parts)


def render_posix(command: AgentCommand, clear: bool = False) -> str:
    """Render for an sh/zsh pane (tmux send-keys). See `render_powershell` on `clear`."""
    parts: list[str] = ["clear"] if clear else []
    for name, value in command.env.items():
        parts.append(f"export {name}={shlex.quote(value)}")
    if command.banner:
        parts.append(f"echo {shlex.quote(command.banner)}")
    parts.append(shlex.join(command.argv))
    return "; ".join(parts)
