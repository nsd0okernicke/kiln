"""
Profile loading — replaces the deleted lib/profile-loader.ps1 and Load-ConfigFromProfile
(both recoverable from git history if the original behaviour ever needs checking).

The PowerShell original round-tripped profile data through generated *source code*: it
emitted `$TERMINAL_0_ROLE = '...'` strings that the caller ran through `Invoke-Expression`,
which is why every value needed manual `'` escaping and why adding a field meant editing
three places. Here a profile parses straight into dataclasses.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from kiln.scheduler.domain.routing import RoutingTable, parse_profile_routing

#: System-wide profile location, per platform. The two shell originals disagreed — the
#: PowerShell one looked in ProgramData, the shell one in /etc — and the first Python port
#: kept only the Windows path, silently dropping the documented Unix location.
SYSTEM_PROFILES_PATH = (
    Path("C:/ProgramData/kiln/profiles.json")
    if os.name == "nt"
    else Path("/etc/kiln/profiles.json")
)

#: Every accepted backend now runs in both modes: a one-shot scheduler adapter
#: (`scheduler/adapters/`) and an interactive wrapper session (`commands.build_agent_command`
#: plus a full template set, which `test_docs_consistency` pins). `grok` was the last to
#: carry only the former.
VALID_AGENTS = ("claude", "copilot", "codex", "grok")
VALID_MODES = ("auto", "manual")

#: Agents with a one-shot adapter in `scheduler/adapters/` -- the deterministic scheduler can
#: only drive a backend it knows how to invoke non-interactively. Every currently-accepted
#: agent has one; a future agent added to VALID_AGENTS without an adapter yet would stay out
#: of this set until it has one too.
SCHEDULER_CAPABLE_AGENTS = ("claude", "copilot", "codex", "grok")

#: Backends whose CLI reports a real dollar figure (`total_cost_usd`). Copilot and Codex do
#: not -- their adapters leave `cost_usd` at the dataclass default of 0.0, and Codex's output
#: contains no dollar amount at all, only token usage. A cost cap on those roles could never
#: fire, which is the worst kind of guard: one that appears to be enforcing. So configuring
#: `maxBudgetUsd` on them fails the launch instead of being silently useless.
COST_REPORTING_AGENTS = ("claude", "grok")

#: Worktree names that mean "work in the project root on the current branch".
CURRENT_DIR_ALIASES = ("@current", "none", "master")

#: Opt-in value for the deterministic Python scheduler, per role.
SCHEDULER_PYTHON = "python"

#: Turns a role entry into a notification pane rather than an agent. It runs
#: the namespaced scheduler inbox against the queue of the role named by `watches`, and has no
#: worktree,
#: no generated instructions, no worker definition and no agent CLI.
SCHEDULER_INBOX = "inbox"

#: Turns a role entry into a live cross-role dashboard rather than an agent. It runs
#: the namespaced scheduler dashboard, aggregating every role in the profile instead of
#: watching one, and
#: has no worktree, no generated instructions, no worker definition and no agent CLI --
#: the same shape as an inbox pane (see `RoleConfig.is_passive`).
SCHEDULER_DASHBOARD = "dashboard"

#: Turns a role entry into the local web cockpit (issue #22) rather than an agent. It runs
#: The HTTP adapter serves one page on 127.0.0.1 over the same `messages.db` and
#: status files the dashboard reads. Same passive shape as `inbox` and `dashboard`, and
#: deliberately alongside them rather than instead: the TTY dashboard is the only view that
#: works over SSH or without a browser.
SCHEDULER_COCKPIT = "cockpit"

VALID_SCHEDULERS = (SCHEDULER_PYTHON, SCHEDULER_INBOX, SCHEDULER_DASHBOARD, SCHEDULER_COCKPIT)


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
    #: Scheduler-mode only. How many times one work item may reach this role before it
    #: escalates instead of working. None means no ceiling, which is the shipped default --
    #: the existing stop conditions all catch failure, and this is the one that catches
    #: expensive success (spec<->code ping-pong runs until a human notices).
    max_cycles: int | None = None
    #: Scheduler-mode only. Dollar ceiling handed to the worker CLI per invocation.
    #: **Only meaningful on a backend that reports cost** -- see `COST_REPORTING_AGENTS`.
    max_budget_usd: float | None = None
    #: Scheduler-mode only. Shell command run in this role's worktree after the worker
    #: reports done and before the handoff. A non-zero exit costs an attempt. Empty means
    #: the role is trusted on its own word, which is the behaviour every role had before.
    #: **This is arbitrary code from the profile, run with the scheduler's privileges** --
    #: the same trust the profile already carries by choosing which agent binaries run.
    verify: str = ""
    #: Seconds before `verify` is killed and treated as a failure.
    verify_timeout: int | None = None
    #: Seconds between polls of this role's queue. Applies to scheduler, inbox and dashboard
    #: panes alike -- all three poll, and all three had the flag with no way to set it.
    poll_interval: float | None = None
    #: Seconds before one worker invocation is abandoned.
    worker_timeout: int | None = None
    #: Silence, not duration -- see adapters.Watchdog. None keeps the module default.
    worker_idle_timeout: float | None = None
    #: Worker attempts per handoff before escalating. Was a `SchedulerContext` dataclass
    #: default with no CLI flag at all -- changeable only from code, despite being one of the
    #: two numbers that decide how an unattended swarm gives up.
    max_attempts: int | None = None
    #: Consecutive escalations before this role stops taking new work. The other one.
    escalation_limit: int | None = None
    #: Dashboard panes only: how many recent messages the activity list shows.
    activity_limit: int | None = None
    #: Inbox panes only: ring the terminal bell on arrival. True is the shipped behaviour.
    bell: bool = True
    #: Cockpit panes only: the port to prefer. The cockpit probes upward when it is taken,
    #: so this is a preference rather than a reservation. None keeps the HTTP adapter's own
    #: default -- one number, in one place, rather than a copy of it here.
    port: int | None = None
    #: Cockpit panes only: open a browser tab at launch. True is the shipped behaviour;
    #: `KILN_COCKPIT_NO_BROWSER` overrides it per machine for headless boxes.
    open_browser: bool = True

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
        kiln.launcher.
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
    def is_cockpit(self) -> bool:
        """
        True for the web cockpit pane rather than an agent.

        Passive like `is_dashboard` -- no agent, no worktree, no generated files -- but it
        binds a port and accepts writes, so it is the one passive pane that can start and
        stop work. That difference lives in what it runs, not in how the launcher treats it.
        """
        return self.scheduler == SCHEDULER_COCKPIT

    @property
    def is_passive(self) -> bool:
        """
        True for any pane that runs no agent at all (inbox, dashboard or cockpit).

        Use this, not the individual `is_*` properties, wherever the decision is
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
        return self.is_inbox or self.is_dashboard or self.is_cockpit

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
    #: A test fixture rather than a profile anyone should pick for real work. Hidden from
    #: `--list-profiles` unless asked for: the profile list is the one menu users choose from,
    #: and entries that exist to exercise Kiln itself do not belong on it.
    fixture: bool = False

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
        paths.append(framework_root / "src" / "kiln" / "resources" / "profiles.json")
    paths += [
        Path.home() / ".kiln" / "profiles.json",
        SYSTEM_PROFILES_PATH,
    ]
    return paths


def find_profiles_config(
    project_root: str | Path, framework_root: str | Path | None = None
) -> Path:
    """Locate profiles.json, preferring a project-local override."""
    candidates = _search_paths(Path(project_root), Path(framework_root) if framework_root else None)
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


#: Every key a terminal entry may carry. Anything else is a typo or a key from a version of
#: Kiln this one is not -- either way the profile does not mean what its author thinks, and
#: silently dropping it is how `"maxAttempts": 5` came to be *accepted and ignored*.
TERMINAL_KEYS = frozenset(
    {
        "role",
        "agent",
        "worktree",
        "title",
        "mode",
        "model",
        "workerModel",
        "scheduler",
        "watches",
        "workerDebug",
        "maxCycles",
        "maxBudgetUsd",
        "verify",
        "verifyTimeout",
        "pollInterval",
        "workerTimeout",
        "workerIdleTimeout",
        "maxAttempts",
        "escalationLimit",
        "activityLimit",
        "bell",
        "port",
        "openBrowser",
    }
)

#: Same, one level up.
PROFILE_KEYS = frozenset({"description", "terminals", "layout", "routing", "defaults", "fixture"})

#: Keys a profile-level `defaults` block may set. Everything a terminal accepts except its
#: identity -- a default `role` would name every terminal the same thing.
DEFAULTS_KEYS = TERMINAL_KEYS - {"role"}


def _reject_unknown_keys(entry: dict, allowed: frozenset[str], context: str) -> None:
    """
    Fail on a key nothing reads, naming it, where it is, and the nearest thing that is real.

    A bare "unknown key" on a profile that worked yesterday is a bad upgrade experience, so
    the message has to do the diagnosis: `"maxAttemps"` is only a useful error if it also
    says `did you mean 'maxAttempts'?`.
    """
    unknown = sorted(set(entry) - allowed)
    if not unknown:
        return
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
        hints.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    raise ProfileError(
        f"{context}: unrecognised key(s) {', '.join(hints)}. "
        f"Valid keys: {', '.join(sorted(allowed))}"
    )


def _parse_role(entry: dict) -> RoleConfig:
    role = str(entry.get("role") or "").strip()
    if not role:
        raise ProfileError("profile contains a terminal entry with no 'role'")

    _reject_unknown_keys(entry, TERMINAL_KEYS, f"role {role!r}")

    agent = _choice(entry, "agent", "claude", VALID_AGENTS, role)
    mode = _choice(entry, "mode", "auto", VALID_MODES, role)
    scheduler = _scheduler(entry, role, agent)
    budget = _budget(entry, role, agent)
    limits = _role_limits(entry, role)

    return RoleConfig(
        role=role,
        agent=agent,
        worktree=_text(entry, "worktree", "@current"),
        title=_text(entry, "title"),
        mode=mode,
        model=_text(entry, "model"),
        worker_model=_text(entry, "workerModel"),
        scheduler=scheduler,
        watches=_text(entry, "watches"),
        worker_debug=bool(entry.get("workerDebug", False)),
        max_cycles=limits["max_cycles"],
        max_budget_usd=budget,
        verify=_text(entry, "verify"),
        verify_timeout=limits["verify_timeout"],
        poll_interval=limits["poll_interval"],
        worker_timeout=limits["worker_timeout"],
        worker_idle_timeout=limits["worker_idle_timeout"],
        max_attempts=limits["max_attempts"],
        escalation_limit=limits["escalation_limit"],
        activity_limit=limits["activity_limit"],
        bell=bool(entry.get("bell", True)),
        port=limits["port"],
        open_browser=bool(entry.get("openBrowser", True)),
    )


def _choice(entry: dict, key: str, default: str, valid: tuple[str, ...], role: str) -> str:
    value = str(entry.get(key) or default).strip()
    if value not in valid:
        raise ProfileError(
            f"unsupported {key} {value!r} for role {role!r}; expected one of " + ", ".join(valid)
        )
    return value


def _text(entry: dict, key: str, default: str = "") -> str:
    return str(entry.get(key) or default).strip()


def _scheduler(entry: dict, role: str, agent: str) -> str | None:
    value = entry.get("scheduler")
    if value is None:
        return None
    scheduler = str(value).strip()
    if scheduler not in VALID_SCHEDULERS:
        raise ProfileError(
            f"unsupported scheduler {scheduler!r} for role {role!r}; expected one of "
            + ", ".join(repr(name) for name in VALID_SCHEDULERS)
        )
    _validate_scheduler_agent(scheduler, role, agent)
    return scheduler


def _validate_scheduler_agent(scheduler: str, role: str, agent: str) -> None:
    if scheduler == SCHEDULER_PYTHON and agent not in SCHEDULER_CAPABLE_AGENTS:
        raise ProfileError(
            f"role {role!r} requests the python scheduler with agent {agent!r}, but it "
            "has no one-shot adapter yet; expected one of "
            + ", ".join(repr(name) for name in SCHEDULER_CAPABLE_AGENTS)
        )


def _budget(entry: dict, role: str, agent: str) -> float | None:
    budget = _positive_or_none(entry.get("maxBudgetUsd"), "maxBudgetUsd", role)
    if budget is not None and agent not in COST_REPORTING_AGENTS:
        raise ProfileError(
            f"role {role!r} sets maxBudgetUsd but agent {agent!r} reports no cost -- its "
            f"adapter always returns $0.00, so the cap could never fire. Cost caps work on: "
            + ", ".join(COST_REPORTING_AGENTS)
        )
    return budget


def _role_limits(entry: dict, role: str) -> dict[str, int | float | None]:
    """Validated numeric profile fields, translated to RoleConfig attribute names."""
    integer = {
        "max_cycles": "maxCycles",
        "verify_timeout": "verifyTimeout",
        "worker_timeout": "workerTimeout",
        "max_attempts": "maxAttempts",
        "escalation_limit": "escalationLimit",
        "activity_limit": "activityLimit",
        "port": "port",
    }
    decimal = {
        "poll_interval": "pollInterval",
        "worker_idle_timeout": "workerIdleTimeout",
    }
    return {
        **{
            attribute: _positive_int_or_none(entry.get(key), key, role)
            for attribute, key in integer.items()
        },
        **{
            attribute: _positive_or_none(entry.get(key), key, role)
            for attribute, key in decimal.items()
        },
    }


def _positive_int_or_none(value: object, key: str, role: str) -> int | None:
    """As `_positive_or_none`, but a whole number -- 2.5 cycles is not a thing."""
    number = _positive_or_none(value, key, role)
    if number is None:
        return None
    if number != int(number):
        raise ProfileError(f"role {role!r}: {key} must be a whole number, got {value!r}")
    return int(number)


def _positive_or_none(value: object, key: str, role: str) -> float | None:
    """
    A ceiling, or None for "no ceiling". Zero and negatives are rejected, not normalised.

    A `maxCycles: 0` that quietly meant "unlimited" would be the worst possible reading of an
    obvious typo -- the operator asked for the tightest possible bound and would get none.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProfileError(f"role {role!r}: {key} must be a number, got {value!r}")
    if value <= 0:
        raise ProfileError(f"role {role!r}: {key} must be greater than zero, got {value!r}")
    return value


def parse_profile(config: dict, name: str) -> Profile:
    """Build a Profile from already-loaded profiles.json content."""
    selected = _selected_profile(config, name)
    defaults = _profile_defaults(selected, name)
    roles, seen = _profile_roles(selected, defaults, name)

    _validate_watches(roles, seen, name)
    layout = selected.get("layout") or {}
    _validate_layout(layout, seen, name)
    routing = _profile_routing(selected, seen, name)

    return Profile(
        name=name,
        description=str(selected.get("description") or ""),
        roles=tuple(roles),
        layout=layout,
        routing=routing,
        fixture=bool(selected.get("fixture", False)),
    )


def _selected_profile(config: dict, name: str) -> dict:
    profiles = config.get("profiles") or {}
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise ProfileError(f"profile {name!r} not found. Available profiles: {available}")

    selected = profiles[name] or {}
    # Before the "defines no terminals" check: a typo'd `terminls` otherwise reports the
    # symptom ("no terminals") instead of the cause.
    _reject_unknown_keys(selected, PROFILE_KEYS, f"profile {name!r}")

    entries = selected.get("terminals") or []
    if not entries:
        raise ProfileError(f"profile {name!r} defines no terminals")
    return selected


def _profile_defaults(selected: dict, name: str) -> dict:
    defaults = selected.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ProfileError(f"profile {name!r}: 'defaults' must be an object")
    _reject_unknown_keys(defaults, DEFAULTS_KEYS, f"profile {name!r} defaults")
    return defaults


def _profile_roles(selected: dict, defaults: dict, name: str) -> tuple[list[RoleConfig], set[str]]:
    roles: list[RoleConfig] = []
    seen: set[str] = set()
    for entry in selected.get("terminals") or []:
        # A terminal's own key always wins. Inheritance is for the values that are the same
        # on every role -- which, in the shipped `full` profile, is the agent and the model
        # repeated five times.
        parsed = _parse_role({**defaults, **entry})
        if parsed.role in seen:
            raise ProfileError(f"duplicate role {parsed.role!r} in profile {name!r}")
        seen.add(parsed.role)
        roles.append(parsed)
    return roles, seen


def _profile_routing(selected: dict, seen: set[str], name: str) -> RoutingTable:
    try:
        routing = parse_profile_routing(selected.get("routing"))
    except ValueError as exc:
        raise ProfileError(f"profile {name!r}: {exc}") from exc
    _validate_routing(routing, seen, name)
    return routing


def _validate_watches(roles: list[RoleConfig], known: set[str], profile_name: str) -> None:
    """
    An inbox must watch a role that exists.

    `watched_role` falls back to the pane's own name when `watches` is unset -- so a *typo*
    silently makes the inbox watch its own queue, which is empty forever and looks exactly
    like a working one.
    """
    for role in roles:
        if role.watches and role.watches not in known:
            raise ProfileError(
                f"profile {profile_name!r}: role {role.role!r} watches {role.watches!r}, "
                f"which is not a role in this profile. Known roles: {', '.join(sorted(known))}"
            )


def _validate_layout(layout: dict, known: set[str], profile_name: str) -> None:
    """
    Every pane in the layout must name a real role.

    The WezTerm Lua matches panes to roles by name and skips a miss silently, so a typo here
    produces a launch that is simply missing a pane -- no error anywhere, and nothing to
    connect the missing agent to the character that caused it.
    """
    for tab in layout.get("tabs") or []:
        for pane in (tab or {}).get("panes") or []:
            name = str((pane or {}).get("role") or "").strip()
            if name and name not in known:
                raise ProfileError(
                    f"profile {profile_name!r}: layout references role {name!r}, which is "
                    f"not in this profile. Known roles: {', '.join(sorted(known))}"
                )


def apply_agent_override(profile: Profile, agent: str, model: str = "") -> Profile:
    """
    Run every agent-bearing role of this profile on a different backend.

    This is what `codex-only` existed to do by hand: it was the same seven roles, the same
    three tabs and the same layout as `full`, differing only in which binary ran them. A
    profile list is the one menu users pick from, and an entry that names a *vendor* rather
    than a kind of work does not belong on it.

    **Models are dropped, not carried.** This is the trap: `full` sets `claude-sonnet-5` on
    every role, and that name means nothing to the Codex CLI -- rewriting only `agent` would
    hand every role a model its backend rejects, and the resulting error would blame the model
    rather than the override. An empty model is the correct configuration for a switched
    backend, and `role_scheduler.resolve_model` already reads it as "let the CLI pick its own
    default" -- which is exactly the state `codex-only` was in, arrived at by hand.

    `model` replaces rather than clears, for callers who know which model they want on the new
    backend. Passive panes run no agent and are left alone.
    """
    if agent not in VALID_AGENTS:
        raise ProfileError(
            f"unsupported agent {agent!r}; expected one of " + ", ".join(VALID_AGENTS)
        )

    rewritten = tuple(
        role if role.is_passive else replace(role, agent=agent, model=model, worker_model=model)
        for role in profile.roles
    )
    for role in rewritten:
        if role.max_budget_usd is not None and role.agent not in COST_REPORTING_AGENTS:
            raise ProfileError(
                f"role {role.role!r} sets maxBudgetUsd, but the override to {agent!r} moves "
                f"it onto a backend that reports no cost, so the cap could never fire. "
                f"Cost caps work on: " + ", ".join(COST_REPORTING_AGENTS)
            )
    return replace(profile, roles=rewritten)


def check_launchable(profile: Profile) -> None:
    """
    Refuse to launch a profile that describes no workflow. Raises ProfileError.

    Separate from `parse_profile` on purpose. Parsing has many callers that build a profile
    to exercise something unrelated -- worktrees, skills, pane commands -- and forcing every
    one of them to declare routing it does not use would add noise to the fixtures without
    catching anything. A *launch* is the moment routing has to exist.

    It has to exist because constitution/workflow.md renders its table from here now, so
    there is no file left to fall back on. Without this check the swarm starts, runs one
    cycle and escalates NO_ROUTE -- a worse place to learn it than the launch that caused it.
    """
    if profile.routing.rules:
        return
    if all(role.is_passive for role in profile.roles):
        return  # nothing that hands off; an inbox/dashboard-only profile needs no routes
    raise ProfileError(
        f"profile {profile.name!r} declares no 'routing'. Every profile owns its own handoff "
        f"routing; add a 'routing' block mapping each role to the role it hands off to."
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
    project_root: str | Path,
    framework_root: str | Path | None = None,
    include_fixtures: bool = False,
) -> list[tuple[str, str]]:
    """
    (name, description) pairs for `--list-profiles`.

    Profiles marked `"fixture": true` are hidden by default. They exist to exercise Kiln
    itself, and this list is the menu a user picks production work from -- `codex-only` and
    `mixed-backends` sat on it looking like choices about *work* when they were choices about
    *backends*. Still launchable by name; just not advertised.
    """
    config = _read_config(find_profiles_config(project_root, framework_root))
    profiles = config.get("profiles") or {}
    return [
        (name, str((body or {}).get("description") or ""))
        for name, body in profiles.items()
        if include_fixtures or not (body or {}).get("fixture", False)
    ]
