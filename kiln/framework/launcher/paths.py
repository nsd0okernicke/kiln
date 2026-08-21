"""
Every path Kiln derives from a project root and the framework checkout.

Centralised because the PowerShell original recomputed these inline at ~20 call sites with
subtly different `Join-Path` chains, which is how `.kiln/tools` and `kiln/framework/tools`
came to be easy to confuse.

Two distinct roots:
  * `project_root`   — the user's project (`-WorkingDir`). Holds `.kiln/`, worktrees, config.
  * `framework_root` — the Kiln checkout itself. Holds templates, the MCP server, scheduler.
They are the same directory only when Kiln is dogfooding itself.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

#: Bare interpreter names to try, in order, for `python_command()`.
PYTHON_CANDIDATES = ("python", "python3")


def python_command() -> str:
    """
    The interpreter name to embed in generated pane commands and `.mcp.json`.

    A *bare* name rather than `sys.executable`, deliberately: each pane's shell and the agent
    CLI spawning the kiln-channel server resolve it from their own PATH at spawn time, which
    is not necessarily the environment the launcher ran in.

    Which bare name is the part that cannot be hardcoded. `python` was, and does not exist on
    a stock Debian/Ubuntu — only `python3` is installed there, so every scheduler pane, the
    inbox, the dashboard and the MCP server died instantly with "Command 'python' not found"
    (confirmed on Ubuntu 24.04). Resolving against PATH keeps Windows on `python` and gives
    Linux/macOS `python3`, with `python3` as the fallback so a miss degrades to the more
    likely name rather than the one already known to be absent.
    """
    for name in PYTHON_CANDIDATES:
        if shutil.which(name):
            return name
    return PYTHON_CANDIDATES[-1]


@dataclass(frozen=True)
class KilnPaths:
    project_root: Path
    framework_root: Path

    @classmethod
    def create(cls, project_root: str | Path, framework_root: str | Path) -> KilnPaths:
        return cls(
            project_root=Path(project_root).expanduser().resolve(),
            framework_root=Path(framework_root).expanduser().resolve(),
        )

    # --- project-owned, ephemeral -------------------------------------------------
    @property
    def state_dir(self) -> Path:
        """`.kiln/` — runtime state, gitignored, symlinked into every worktree."""
        return self.project_root / ".kiln"

    @property
    def worktrees_dir(self) -> Path:
        return self.project_root / ".worktrees"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "messages.db"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def traffic_db(self) -> Path:
        """
        `.kiln/traffic.db` — the proxy's capture store.

        Deliberately not `messages.db`: that file is live swarm state, queried by the
        dashboard, the inbox and the `kiln-db` MCP server, and small enough to open in a
        SQLite browser. Captured request bodies are orders of magnitude larger.
        """
        return self.state_dir / "traffic.db"

    @property
    def status_dir(self) -> Path:
        """Per-role `<role>.json` read by the WezTerm status bar."""
        return self.state_dir / "status"

    @property
    def state_tools_dir(self) -> Path:
        """`.kiln/tools/` — framework tools copied in fresh on every launch."""
        return self.state_dir / "tools"

    @property
    def sessions_file(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def codex_home_dir(self) -> Path:
        return self.state_dir / "codex-home"

    # --- project-owned, version-controlled ----------------------------------------
    @property
    def kiln_dir(self) -> Path:
        """`kiln/` — version-controlled, NOT to be blanket-gitignored."""
        return self.project_root / "kiln"

    @property
    def kiln_project_dir(self) -> Path:
        return self.kiln_dir / "project"

    @property
    def constitution_dir(self) -> Path:
        return self.kiln_project_dir / "constitution"

    @property
    def workflow_md(self) -> Path:
        return self.constitution_dir / "workflow.md"

    @property
    def roles_dir(self) -> Path:
        return self.kiln_project_dir / "roles"

    @property
    def skills_dir(self) -> Path:
        return self.kiln_project_dir / "skills"

    # --- framework-owned ----------------------------------------------------------
    @property
    def bundled_dir(self) -> Path:
        return self.framework_root / "kiln"

    @property
    def framework_dir(self) -> Path:
        return self.bundled_dir / "framework"

    @property
    def templates_dir(self) -> Path:
        return self.framework_dir / "templates"

    @property
    def framework_tools_dir(self) -> Path:
        return self.framework_dir / "tools"

    @property
    def framework_profiles_json(self) -> Path:
        return self.framework_dir / "profiles.json"

    @property
    def channel_script(self) -> Path:
        """Referenced by absolute path in generated .mcp.json — never copied."""
        return self.framework_dir / "mcp-server" / "channel.py"

    @property
    def python_package_root(self) -> Path:
        """Goes on PYTHONPATH so `scheduler` and `launcher` are importable."""
        return self.framework_dir

    @property
    def claude_settings_template(self) -> Path:
        return self.bundled_dir / ".claude" / "settings.json"

    # --- per-role -----------------------------------------------------------------
    def worktree_path(self, worktree_name: str) -> Path:
        return self.worktrees_dir / worktree_name

    def channel_log(self, role: str) -> Path:
        return self.logs_dir / f"channel-{role}.log"

    def agent_debug_log(self, role: str, agent: str = "claude") -> Path:
        """
        Where a wrapper-mode CLI writes its own internal trace (`--debug-file`).

        Named for the backend, not hardcoded to `claude`: `grok` takes the same flag, and a
        grok role writing to `claude-debug-<role>.log` would send anyone diagnosing a stall
        (README's troubleshooting steps name this file by path) to a log for the wrong CLI.
        The default keeps every existing Claude path and filename unchanged.
        """
        return self.logs_dir / f"{agent}-debug-{role}.log"

    def scheduler_log(self, role: str) -> Path:
        return self.logs_dir / f"scheduler-{role}.log"

    def codex_home(self, role: str) -> Path:
        return self.codex_home_dir / role

    def worker_agent_file(self, role: str, agent: str) -> Path:
        """
        Where Write-GeneratedWorkerAgent puts a role's worker definition.

        Each CLI discovers project-scoped agents in its own location, so this is per-agent
        rather than one shared path. Grok's file is the same frontmatter-markdown *format* as
        Claude's (both consume it via an inline `--agents` JSON payload built from the same
        `worker_prompt.build_agents_payload()`), but gets its own directory rather than
        sharing `.claude/agents/` -- a separate namespace per backend, matching Copilot/Codex.
        """
        if agent == "copilot":
            return self.project_root / ".github" / "agents" / f"{role}-worker.agent.md"
        if agent == "codex":
            return self.project_root / ".codex" / "agents" / f"{role}-worker.toml"
        if agent == "grok":
            return self.project_root / ".grok" / "agents" / f"{role}-worker.md"
        return self.project_root / ".claude" / "agents" / f"{role}-worker.md"
