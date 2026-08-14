"""
Profile loading — replaces the deleted lib/profile-loader.ps1 and Load-ConfigFromProfile
(both recoverable from git history if the original behaviour ever needs checking).

The PowerShell original round-tripped profile data through generated *source code*: it
emitted `$TERMINAL_0_ROLE = '...'` strings that the caller ran through `Invoke-Expression`,
which is why every value needed manual `'` escaping and why adding a field meant editing
three places. Here a profile parses straight into dataclasses.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from scheduler.routing import RoutingTable, parse_profile_routing

#: System-wide profile location, per platform. The two shell originals disagreed — the
#: PowerShell one looked in ProgramData, the shell one in /etc — and the first Python port
#: kept only the Windows path, silently dropping the documented Unix location.
SYSTEM_PROFILES_PATH = (
    Path("C:/ProgramData/kiln/profiles.json") if os.name == "nt"
    else Path("/etc/kiln/profiles.json")
)

#: `grok` has a scheduler adapter (`scheduler/adapters/grok_adapter.py`) but no *wrapper*-mode
#: launch implementation (see README's Known Limitations) -- it is listed so an existing
#: profile does not fail validation for a wrapper-mode role.
VALID_AGENTS = ("claude", "copilot", "codex", "grok")
VALID_MODES = ("auto", "manual")

#: Agents with a one-shot adapter in `scheduler/adapters/` -- the deterministic scheduler can
#: only drive a backend it knows how to invoke non-interactively. Every currently-accepted
#: agent has one; a future agent added to VALID_AGENTS without an adapter yet would stay out
#: of this set until it has one too (same path `grok` followed here).
SCHEDULER_CAPABLE_AGENTS = ("claude", "copilot", "codex", "grok")

#: Worktree names that mean "work in the project root on the current branch".
CURRENT_DIR_ALIASES = ("@current", "none", "master")

#: Opt-in value for the deterministic Python scheduler, per role.
SCHEDULER_PYTHON = "python"

#: Turns a role entry into a notification pane rather than an agent. It runs
#: `scheduler.inbox` against the queue of the role named by `watches`, and has no worktree,
#: no generated instructions, no worker definition and no agent CLI.
SCHEDULER_INBOX = "inbox"

#: Turns a role entry into a live cross-role dashboard rather than an agent. It runs
#: `scheduler.dashboard`, aggregating every role in the profile instead of watching one, and
#: has no worktree, no generated instructions, no worker definition and no agent CLI --
#: the same shape as an inbox pane (see `RoleConfig.is_passive`).
SCHEDULER_DASHBOARD = "dashboard"

VALID_SCHEDULERS = (SCHEDULER_PYTHON, SCHEDULER_INBOX, SCHEDULER_DASHBOARD)


class ProfileError(Exception):
    """Raised for a malformed or missing profile — always fatal, never guessed around."""


@dataclass(frozen=True)
class RoleConfig:
    role: str
    agent: str = "claude"
    worktree: str = "@current"
    title: str = ""
    mode: str = "auto"
    model: str = ""
    worker_model: str = ""
    #: "python" opts this role into the deterministic scheduler, "inbox" makes it a
    #: notification pane; None keeps the LLM wrapper.
    scheduler: str | None = None
    #: Whose queue an inbox pane watches. Ignored for every other kind of role.
    watches: str = ""
    #: Scheduler-mode only: write the backend CLI's own internal debug trace per attempt
    #: (Claude's `--debug-file`, Copilot's `--log-dir`/`--log-level all`) to `.kiln/logs/`.
    #: Off by default -- it's substantial volume for a healthy run, worth paying for only
    #: while actively diagnosing a failure like the copilot "permission denied" investigation.
    worker_debug: bool = False

    @property
    def uses_current_dir(self) -> bool:
        return self.worktree in CURRENT_DIR_ALIASES

    @property
    def display_name(self) -> str:
        """`human-in-the-loop` -> `Human In The Loop`."""
        parts = self.role.replace("_", "-").split("-")
        return " ".join(part.capitalize() for part in parts if part)

    @property
    def session_name(self) -> str:
        return f"kiln-{self.role}"

    @property
    def uses_scheduler(self) -> bool:
        """
        True only for auto-mode roles that opted in.

        Manual roles hold real conversations and need human approval each cycle, so they
        stay interactive LLM sessions no matter what the flag says.
        """
        return self.scheduler == SCHEDULER_PYTHON and self.mode == "auto"

    @property
    def is_inbox(self) -> bool:
        """
        True for a notification pane rather than an agent.

        An inbox has no agent, no worktree and no generated files. Every per-role step in
        the launch sequence has to skip it, so this is checked in a lot of places — the
        alternative was a second, parallel notion of "pane" running through the whole
        launcher.
        """
        return self.scheduler == SCHEDULER_INBOX

    @property
    def is_dashboard(self) -> bool:
        """
        True for a cross-role dashboard pane rather than an agent.

        Same shape as `is_inbox` -- no agent, no worktree, no generated files -- except it
        aggregates every role in the profile instead of watching one, so it has no `watches`
        equivalent.
        """
        return self.scheduler == SCHEDULER_DASHBOARD

    @property
    def is_passive(self) -> bool:
        """
        True for any pane that runs no agent at all (inbox or dashboard).

        Use this, not `is_inbox`/`is_dashboard` individually, wherever the decision is
        simply "does this role get normal per-role generation" (instructions, worker
        definitions, `.mcp.json` ownership). A role that shares its worktree with a real
        role -- which every passive pane does, by design -- must never be treated as owning
        files at that path, or one passive-pane type's cleanup can delete another role's real
        file out from under it (observed live: an `inbox` role deleted the `human-in-the-loop`
        role's just-written CLAUDE.md this same session, because only `is_inbox` was checked
        in `generate.write_instructions` and nothing generalized the pattern before a second
        passive-pane type was added). Only check `is_inbox`/`is_dashboard` directly where the
        code needs to know *which* passive pane it is, e.g. which command to build.
        """
        return self.is_inbox or self.is_dashboard

    @property
    def watched_role(self) -> str:
        """The queue an inbox shows. Defaults to its own name."""
        return self.watches or self.role

    def branch_name(self, base_branch: str) -> str:
        """Sub-branch for this role's worktree; `@current` roles stay on the base branch."""
        return base_branch if self.uses_current_dir else f"{base_branch}-{self.worktree}"


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    roles: tuple[RoleConfig, ...] = ()
    layout: dict = field(default_factory=dict)
    #: This profile's own handoff routing, replacing constitution/workflow.md's table.
    #:
    #: Empty means "use workflow.md", which is what every role-complete profile does. A
    #: profile with a different *shape* needs its own, because there is only one table in
    #: the file and two shapes cannot both define the same role's default row — see
    #: `scheduler.routing.parse_profile_routing`.
    routing: RoutingTable = field(default_factory=RoutingTable)

    def role(self, name: str) -> RoleConfig | None:
        return next((r for r in self.roles if r.role == name), None)

    @property
    def current_dir_role(self) -> RoleConfig | None:
        """
        First *agent* working in the project root — it owns the root `.mcp.json`.

        Passive panes (inbox, dashboard) also live in the project root but run no agent, so
        one listed ahead of the human's session would otherwise steal ownership and leave the
        real role with no MCP config at all.
        """
        return next((r for r in self.roles if r.uses_current_dir and not r.is_passive), None)

    def has_agent(self, agent: str) -> bool:
        return any(r.agent == agent for r in self.roles)

    def inbox_watches(self, role_name: str) -> bool:
        """True when some `inbox` pane in this profile watches `role_name`'s queue."""
        return any(r.is_inbox and r.watched_role == role_name for r in self.roles)


def _search_paths(project_root: Path, framework_root: Path | None) -> list[Path]:
    """Cascading lookup: project override first, framework default last."""
    paths = [
        project_root / "kiln.profiles.json",
        project_root / "kiln" / "profiles.json",
        project_root / ".kiln" / "profiles.json",
    ]
    if framework_root:
        paths.append(framework_root / "kiln" / "framework" / "profiles.json")
    paths += [
        Path.home() / ".kiln" / "profiles.json",
        SYSTEM_PROFILES_PATH,
    ]
    return paths


def find_profiles_config(
    project_root: str | Path, framework_root: str | Path | None = None
) -> Path:
    """Locate profiles.json, preferring a project-local override."""
    candidates = _search_paths(
        Path(project_root), Path(framework_root) if framework_root else None
    )
    for path in candidates:
        if path.is_file():
            return path
    listed = ", ".join(str(p) for p in candidates)
    raise ProfileError(f"could not find profiles.json. Searched: {listed}")


def _read_config(config_path: Path) -> dict:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"could not read {config_path}: {exc}") from exc


def default_profile_name(
    project_root: str | Path,
    framework_root: str | Path | None = None,
    fallback: str = "default",
) -> str:
    """The profile named by `default`, falling back rather than failing."""
    try:
        config = _read_config(find_profiles_config(project_root, framework_root))
    except ProfileError:
        return fallback
    return str(config.get("default") or fallback)


def _parse_role(entry: dict) -> RoleConfig:
    role = str(entry.get("role") or "").strip()
    if not role:
        raise ProfileError("profile contains a terminal entry with no 'role'")

    agent = str(entry.get("agent") or "claude").strip()
    if agent not in VALID_AGENTS:
        raise ProfileError(
            f"unsupported agent {agent!r} for role {role!r}; expected one of "
            + ", ".join(VALID_AGENTS)
        )

    mode = str(entry.get("mode") or "auto").strip()
    if mode not in VALID_MODES:
        raise ProfileError(
            f"unsupported mode {mode!r} for role {role!r}; expected one of "
            + ", ".join(VALID_MODES)
        )

    scheduler = entry.get("scheduler")
    if scheduler is not None:
        scheduler = str(scheduler).strip()
        if scheduler not in VALID_SCHEDULERS:
            raise ProfileError(
                f"unsupported scheduler {scheduler!r} for role {role!r}; expected one of "
                + ", ".join(repr(name) for name in VALID_SCHEDULERS)
            )
        # Fail loudly rather than silently running an unsupported backend's worker through
        # an adapter that does not exist yet. An inbox runs no agent, so it is exempt.
        if scheduler == SCHEDULER_PYTHON and agent not in SCHEDULER_CAPABLE_AGENTS:
            raise ProfileError(
                f"role {role!r} requests the python scheduler with agent {agent!r}, but it "
                "has no one-shot adapter yet; expected one of "
                + ", ".join(repr(name) for name in SCHEDULER_CAPABLE_AGENTS)
            )

    return RoleConfig(
        role=role,
        agent=agent,
        worktree=str(entry.get("worktree") or "@current").strip(),
        title=str(entry.get("title") or "").strip(),
        mode=mode,
        model=str(entry.get("model") or "").strip(),
        worker_model=str(entry.get("workerModel") or "").strip(),
        scheduler=scheduler,
        watches=str(entry.get("watches") or "").strip(),
        worker_debug=bool(entry.get("workerDebug", False)),
    )


def parse_profile(config: dict, name: str) -> Profile:
    """Build a Profile from already-loaded profiles.json content."""
    profiles = config.get("profiles") or {}
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise ProfileError(f"profile {name!r} not found. Available profiles: {available}")

    selected = profiles[name] or {}
    entries = selected.get("terminals") or []
    if not entries:
        raise ProfileError(f"profile {name!r} defines no terminals")

    roles: list[RoleConfig] = []
    seen: set[str] = set()
    for entry in entries:
        parsed = _parse_role(entry)
        if parsed.role in seen:
            raise ProfileError(f"duplicate role {parsed.role!r} in profile {name!r}")
        seen.add(parsed.role)
        roles.append(parsed)

    try:
        routing = parse_profile_routing(selected.get("routing"))
    except ValueError as exc:
        raise ProfileError(f"profile {name!r}: {exc}") from exc
    _validate_routing(routing, seen, name)

    return Profile(
        name=name,
        description=str(selected.get("description") or ""),
        roles=tuple(roles),
        layout=selected.get("layout") or {},
        routing=routing,
    )


def _validate_routing(routing: RoutingTable, roles: set[str], profile_name: str) -> None:
    """
    Every role named in a profile's routing must exist in that profile.

    A route to a role the profile never launches resolves to a target whose queue nobody
    polls: the handoff inserts, the cycle reports success, and the work stops dead with no
    error anywhere. That is the same silent class as a `watches` naming a missing role, and
    it is worth catching at load rather than three cycles into a run.
    """
    for rule in routing.rules:
        for field_name, value in (("role", rule.role), ("target", rule.target)):
            if value not in roles:
                known = ", ".join(sorted(roles))
                raise ProfileError(
                    f"profile {profile_name!r}: routing {field_name} {value!r} is not a role "
                    f"in this profile. Roles: {known}"
                )
        if rule.when_sender is not None and rule.when_sender not in roles:
            known = ", ".join(sorted(roles))
            raise ProfileError(
                f"profile {profile_name!r}: routing condition 'when sender is "
                f"{rule.when_sender!r}' names no role in this profile. Roles: {known}"
            )


def load_profile(
    project_root: str | Path,
    framework_root: str | Path | None = None,
    name: str | None = None,
    config_path: str | Path | None = None,
) -> Profile:
    """Locate, read and parse a profile."""
    path = Path(config_path) if config_path else find_profiles_config(project_root, framework_root)
    config = _read_config(path)
    resolved = name or str(config.get("default") or "default")
    return parse_profile(config, resolved)


def list_profiles(
    project_root: str | Path, framework_root: str | Path | None = None
) -> list[tuple[str, str]]:
    """(name, description) pairs for `--list-profiles`."""
    config = _read_config(find_profiles_config(project_root, framework_root))
    profiles = config.get("profiles") or {}
    return [(name, str((body or {}).get("description") or "")) for name, body in profiles.items()]
