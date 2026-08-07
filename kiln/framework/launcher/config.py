"""
Profile loading — replaces lib/profile-loader.ps1 and Load-ConfigFromProfile.

The PowerShell original round-tripped profile data through generated *source code*: it
emitted `$TERMINAL_0_ROLE = '...'` strings that the caller ran through `Invoke-Expression`,
which is why every value needed manual `'` escaping and why adding a field meant editing
three places. Here a profile parses straight into dataclasses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: `grok` is accepted by config but has no launch implementation yet (see README's
#: Known Limitations); it is listed so an existing profile does not fail validation.
VALID_AGENTS = ("claude", "copilot", "codex", "grok")
VALID_MODES = ("auto", "manual")

#: Worktree names that mean "work in the project root on the current branch".
CURRENT_DIR_ALIASES = ("@current", "none", "master")

#: Opt-in value for the deterministic Python scheduler, per role.
SCHEDULER_PYTHON = "python"


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
    #: "python" opts this role into the deterministic scheduler; None keeps the LLM wrapper.
    scheduler: str | None = None

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

    def branch_name(self, base_branch: str) -> str:
        """Sub-branch for this role's worktree; `@current` roles stay on the base branch."""
        return base_branch if self.uses_current_dir else f"{base_branch}-{self.worktree}"


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    roles: tuple[RoleConfig, ...] = ()
    layout: dict = field(default_factory=dict)

    def role(self, name: str) -> RoleConfig | None:
        return next((r for r in self.roles if r.role == name), None)

    @property
    def current_dir_role(self) -> RoleConfig | None:
        """First role working in the project root — it owns the root `.mcp.json`."""
        return next((r for r in self.roles if r.uses_current_dir), None)

    def has_agent(self, agent: str) -> bool:
        return any(r.agent == agent for r in self.roles)


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
        Path("C:/ProgramData/kiln/profiles.json"),
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
        if scheduler != SCHEDULER_PYTHON:
            raise ProfileError(
                f"unsupported scheduler {scheduler!r} for role {role!r}; "
                f"expected {SCHEDULER_PYTHON!r}"
            )
        # Fail loudly rather than silently running an unsupported backend's worker through
        # an adapter that does not exist yet.
        if agent != "claude":
            raise ProfileError(
                f"role {role!r} requests the python scheduler with agent {agent!r}, but only "
                "'claude' has a validated one-shot adapter so far"
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

    return Profile(
        name=name,
        description=str(selected.get("description") or ""),
        roles=tuple(roles),
        layout=selected.get("layout") or {},
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
