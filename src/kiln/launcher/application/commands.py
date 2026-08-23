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

from kiln.scheduler.domain.routing import format_routing_rules

from ..domain.paths import KilnPaths, python_command
from ..domain.profile import Profile, RoleConfig

#: Fallback for the cockpit's `--human-role` when a profile declares no manual role. The
#: conventional name every shipped profile and the routing table already use.
DEFAULT_HUMAN_ROLE = "human-in-the-loop"

#: Opening prompt handed to an interactive wrapper session.
START_PROMPT = "Start your role session."

#: Fallback when a Claude role omits `model` in its profile entry.
DEFAULT_CLAUDE_MODEL = "sonnet"

#: Backends whose traffic can be routed through the local kiln.proxy.
#:
#: Both entries are *verified live*, not assumed: each CLI honours the override and still
#: attaches its own subscription credential to a local, non-vendor host — Claude via OAuth,
#: Codex via its ChatGPT token. `grok` is unspiked and `copilot` talks to GitHub's endpoints
#: with no override anyone has found, so routing either would be a guess that silently does
#: nothing or breaks their auth.
PROXY_CAPABLE_AGENTS = frozenset({"claude", "codex"})

#: Kiln-owned bridge variable used to carry a Codex proxy URL into both interactive and
#: one-shot invocations.  Translating that value into Codex CLI flags happens at each
#: adapter edge; the application layer only decides whether a role should receive it.
CODEX_PROXY_BASE_URL_ENV = "KILN_PROXY_BASE_URL"

#: Env var each proxy-capable backend reads for its API base URL.
#:
#: Codex is the odd one: it has no base-URL variable of its own and needs `-c` overrides on
#: the command line instead. Kiln carries the URL in its own variable and each Codex call
#: translates it into Codex `-c` flags, so the transport stays uniform —
#: pane environment, inherited by the one-shot worker — while the CLI-specific spelling
#: lives with the adapter.
PROXY_BASE_URL_VARS = {
    "claude": "ANTHROPIC_BASE_URL",
    "codex": CODEX_PROXY_BASE_URL_ENV,
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
    cannot answer "what did the refactorer cost". The proxy HTTP adapter strips the
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
        "--model",
        role.model or DEFAULT_CLAUDE_MODEL,
        "--permission-mode",
        permission_mode,
        "--mcp-config",
        "./.mcp.json",
        "--debug-file",
        str(paths.agent_debug_log(role.role)),
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


def _grok_command(role: RoleConfig, paths: KilnPaths) -> AgentCommand:
    """
    An interactive grok wrapper session.

    Every flag verified live against `grok` 1.0.5, the same methodology the scheduler adapter
    used. The two non-obvious choices:

    - `--permission-mode`, not the adapter's `--always-approve`. Both exist on this CLI and
      both auto-approve, but only `--permission-mode` can also express *not* auto-approving,
      which is what a `manual` role needs — it is human-supervised, and silently bypassing its
      approvals would be wrong. The value names match Claude's exactly.
    - No `--no-subagents`. The adapter passes it to isolate a one-shot worker from recursive
      delegation; a wrapper is the opposite case. It reaches `<role>-worker` through grok's
      `spawn_subagent` tool, so disabling that would leave it no way to delegate and force it
      to do the role's work itself — the failure this whole pattern exists to prevent.

    No `--agents` payload either, unlike the adapter: a wrapper session discovers
    `.grok/agents/<role>-worker.md` from the worktree as a *project* agent (verified via
    `grok inspect`), which is the file `generate.write_worker_file` already writes there.

    MCP is not on the argv at all — this CLI has no `--mcp-config` equivalent. It reads the
    worktree's `.mcp.json` directly, the same file Claude is pointed at explicitly, so
    `workspace.prepare_worktrees` has already wired this role's servers by the time the pane
    opens. That file carries `kiln-db` only for a grok role: see
    `generate.BLOCKING_CHANNEL_AGENTS` for why the channel is withheld.
    """
    permission_mode = "default" if role.mode == "manual" else "bypassPermissions"
    argv = [
        "grok",
        "--permission-mode",
        permission_mode,
        "--debug-file",
        str(paths.agent_debug_log(role.role, "grok")),
    ]
    if role.model:
        argv += ["-m", role.model]
    argv.append(START_PROMPT)
    # Banner for the same reason Copilot gets one: no `-n`/`--name` flag, so nothing in the
    # session itself says which role this pane is.
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
    argv += _codex_proxy_config_args(_proxy_base_url(role, proxy_url))
    argv.append(START_PROMPT)
    return AgentCommand(
        argv=argv,
        env={"CODEX_HOME": str(paths.codex_home(role.role))},
    )


def _proxy_base_url(role: RoleConfig, proxy_url: str | None) -> str | None:
    """The role's proxy URL, or None when this role is not routed."""
    return proxy_env(role, proxy_url).get(PROXY_BASE_URL_VARS.get(role.agent, ""))


def _codex_proxy_config_args(base_url: str | None) -> list[str]:
    """Render Codex's CLI-specific base-URL override at the command adapter edge."""
    if not base_url:
        return []
    provider = "model_providers.kiln"
    return [
        "-c",
        "model_provider=kiln",
        "-c",
        f'{provider}.name="kiln"',
        "-c",
        f'{provider}.base_url="{base_url.rstrip("/")}"',
        "-c",
        f'{provider}.wire_api="responses"',
    ]


def _scheduler_options(role: RoleConfig, profile: Profile | None) -> list[str]:
    options = _routing_options(profile)
    options += _optional_arg("--model", role.worker_model or role.model)
    options += ["--worker-debug"] if role.worker_debug else []
    options += _optional_arg("--max-cycles", role.max_cycles)
    options += _optional_arg("--max-budget-usd", role.max_budget_usd)
    options += _verification_options(role)
    return options


def _routing_options(profile: Profile | None) -> list[str]:
    options = []
    if profile is not None and profile.routing.rules:
        for rule in format_routing_rules(profile.routing):
            options += ["--route", rule]
    return options


def _optional_arg(flag: str, value: object) -> list[str]:
    return [] if value is None or value == "" else [flag, str(value)]


def _verification_options(role: RoleConfig) -> list[str]:
    options = []
    if role.verify:
        options += ["--verify", role.verify]
        if role.verify_timeout is not None:
            options += ["--verify-timeout", str(role.verify_timeout)]
    return options


def _scheduler_command(
    role: RoleConfig, paths: KilnPaths, branch: str, profile: Profile | None = None
) -> AgentCommand:
    """
    Launch the deterministic scheduler instead of an LLM wrapper session.

    Invoked as `python -m kiln.scheduler.infrastructure.cli.role_scheduler`, not as a bare
    script path: the package
    uses relative imports, so running the file directly fails with "attempted relative
    import with no known parent package". PYTHONPATH points at src so both
    `scheduler` and `launcher` resolve.
    """
    argv = [
        python_command(),
        "-m",
        "kiln.scheduler.infrastructure.cli.role_scheduler",
        "--role",
        role.role,
        "--branch",
        branch,
        "--db-path",
        str(paths.db_path),
        "--worktree",
        str(_worktree_for(role, paths)),
        "--workflow",
        str(paths.workflow_md),
        "--worker-agent",
        str(paths.worker_agent_file(role.role, role.agent)),
        "--agent",
        role.agent,
    ]
    # A profile that declares its own routing replaces workflow.md's table -- the scheduler
    # runs in its own process and cannot read the launcher's parsed profile, so the resolved
    # rules travel as arguments. `--workflow` stays either way: a profile with no routing of
    # its own keeps reading the file, which is what every role-complete profile does.
    argv += _scheduler_options(role, profile)
    argv += _tuning_args(
        role,
        {
            "--poll-interval": role.poll_interval,
            "--worker-timeout": role.worker_timeout,
            "--worker-idle-timeout": role.worker_idle_timeout,
            "--max-attempts": role.max_attempts,
            "--escalation-limit": role.escalation_limit,
        },
    )

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


def _tuning_args(role: RoleConfig, knobs: dict[str, object]) -> list[str]:
    """
    Flags for the knobs this role actually set, skipping the ones it left alone.

    Passing only what was configured means an unset knob keeps the *module's* default rather
    than a copy of that default written here -- two places to change one number is how they
    drift. Every one of these flags already existed on the scheduler, inbox and dashboard;
    what was missing was any way to reach them from a profile, so `"pollInterval": 10` was
    accepted and ignored.
    """
    argv: list[str] = []
    for flag, value in knobs.items():
        if value is not None:
            argv += [flag, f"{value:g}" if isinstance(value, float) else str(value)]
    return argv


def _inbox_command(role: RoleConfig, paths: KilnPaths, branch: str) -> AgentCommand:
    """
    Launch the human's notification pane.

    Watches another role's queue rather than its own: the pane is called `inbox`, but the
    messages it shows are addressed to `human-in-the-loop`.
    """
    argv = [
        python_command(),
        "-m",
        "kiln.scheduler.infrastructure.cli.inbox",
        "--role",
        role.watched_role,
        "--branch",
        branch,
        "--db-path",
        str(paths.db_path),
        # The human is a real role in the graph, not just a notification target: an inbound
        # handoff must be merged into their tree or the work they are asked to review is not
        # actually there. This is /kiln-receive steps 1 and 4, done deterministically.
        "--worktree",
        str(_worktree_for(role, paths)),
        "--log-file",
        str(paths.scheduler_log(role.role)),
    ]
    argv += _tuning_args(role, {"--poll-interval": role.poll_interval})
    if not role.bell:
        argv.append("--no-bell")
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
        python_command(),
        "-m",
        "kiln.scheduler.infrastructure.cli.dashboard",
        "--branch",
        branch,
        "--db-path",
        str(paths.db_path),
        "--status-dir",
        str(paths.status_dir),
        "--sessions-file",
        str(paths.sessions_file),
        "--project-name",
        paths.project_root.name,
        "--log-file",
        str(paths.scheduler_log(role.role)),
        # Always passed, never conditional on the proxy being enabled: the dashboard hides
        # the panel when the store is absent, so one code path covers both cases and a
        # swarm launched without the proxy is not a different dashboard.
        "--traffic-db",
        str(paths.traffic_db),
    ]
    argv += _tuning_args(
        role,
        {
            "--poll-interval": role.poll_interval,
            "--activity-limit": role.activity_limit,
        },
    )
    return AgentCommand(
        argv=argv,
        env={
            "PYTHONPATH": str(paths.python_package_root),
            "PYTHONIOENCODING": "utf-8",
        },
    )


def _cockpit_command(
    role: RoleConfig, paths: KilnPaths, branch: str, profile: Profile | None
) -> AgentCommand:
    """
    Launch the local web cockpit pane (issue #22).

    Reads the same shared DB, status directory and sessions file the dashboard pane does --
    it is a second front end over one set of numbers, not a second source of them.

    Two things it needs that the dashboard does not, and both come from the profile because
    the cockpit deliberately does not parse profiles itself:

    * `--lanes`, the roles that get a swimlane. Taken from the routing table rather than from
      every role in the profile: `inbox`, `dashboard` and the cockpit itself hand nothing off,
      so a lane for them would be permanently empty by construction.
    * `--intake-role`, where `New Task` sends. That is exactly what routing says the human
      hands off to, so asking the table is the only answer that cannot disagree with the
      swarm's actual first hop.
    """
    human_role = _human_role(profile)
    argv = [
        python_command(),
        "-m",
        "kiln.cockpit.infrastructure.http.server",
        "--branch",
        branch,
        "--db-path",
        str(paths.db_path),
        "--status-dir",
        str(paths.status_dir),
        "--sessions-file",
        str(paths.sessions_file),
        "--project-name",
        paths.project_root.name,
        "--url-file",
        str(paths.cockpit_url_file),
        "--pid-file",
        str(paths.cockpit_pid_file),
        "--log-file",
        str(paths.scheduler_log(role.role)),
        # As with the dashboard: always passed, and hidden by the cockpit when the store is
        # absent, so a swarm launched without the proxy is not a different kiln.cockpit.
        "--traffic-db",
        str(paths.traffic_db),
        "--human-role",
        human_role,
    ]
    lanes = _cockpit_lanes(profile)
    if lanes:
        argv += ["--lanes", ",".join(lanes)]
    intake = profile.routing.resolve(human_role) if profile else None
    if intake:
        argv += ["--intake-role", intake]
    argv += _tuning_args(
        role,
        {
            "--port": role.port,
            "--activity-limit": role.activity_limit,
        },
    )
    if not role.open_browser:
        argv.append("--no-browser")
    return AgentCommand(
        argv=argv,
        env={
            "PYTHONPATH": str(paths.python_package_root),
            "PYTHONIOENCODING": "utf-8",
        },
    )


def _human_role(profile: Profile | None) -> str:
    """
    Whose queue the cockpit treats as the human's.

    The first manual, non-passive role in the profile, which is what "the person operating
    this swarm" means structurally -- a manual role is one that holds conversations and needs
    approval each cycle. Falls back to the conventional name so a profile with no manual role
    still produces a cockpit that runs rather than one that refuses to start.
    """
    if profile:
        for candidate in profile.roles:
            if candidate.mode == "manual" and not candidate.is_passive:
                return candidate.role
    return DEFAULT_HUMAN_ROLE


def _cockpit_lanes(profile: Profile | None) -> list[str]:
    """
    Board lanes, in profile order, restricted to roles that participate in routing.

    Profile order rather than routing order: the profile lists roles in the direction work
    flows, while the routing table is a set of rules whose iteration order is an accident of
    how the JSON was written.
    """
    if not profile:
        return []
    participating = set(profile.routing.roles())
    for rule in profile.routing.rules:
        participating.add(rule.target)
    return [
        role.role for role in profile.roles if not role.is_passive and role.role in participating
    ]


def _worktree_for(role: RoleConfig, paths: KilnPaths) -> Path:
    return paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)


def build_agent_command(
    role: RoleConfig,
    paths: KilnPaths,
    branch: str,
    proxy_url: str | None = None,
    profile: Profile | None = None,
) -> AgentCommand:
    """
    Build the pane command for one role.

    Scheduler-enabled roles bypass the agent CLI entirely — the scheduler invokes the worker
    itself, one shot per handoff.

    `proxy_url` routes this role's API traffic through the local capture kiln.proxy. It is applied
    to both scheduler and wrapper roles: a scheduler-mode worker is a subprocess of its pane
    and inherits the pane's environment, so setting it once on the pane covers both.
    """
    if role.is_inbox:
        return _inbox_command(role, paths, branch)

    if role.is_dashboard:
        return _dashboard_command(role, paths, branch)

    if role.is_cockpit:
        return _cockpit_command(role, paths, branch, profile)

    if role.uses_scheduler:
        return _scheduler_command(role, paths, branch, profile).with_env(
            **proxy_env(role, proxy_url)
        )

    if role.agent == "claude":
        return _claude_command(role, paths).with_env(**proxy_env(role, proxy_url))
    if role.agent == "copilot":
        return _copilot_command(role)
    if role.agent == "codex":
        return _codex_command(role, paths, proxy_url).with_env(**proxy_env(role, proxy_url))
    if role.agent == "grok":
        return _grok_command(role, paths)

    # Every agent `config.VALID_AGENTS` accepts is handled above, so this is only reachable
    # for one added there without a launch path yet. Say so in the pane rather than failing
    # the whole swarm launch for one role.
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
