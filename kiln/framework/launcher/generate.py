"""
Generated per-role files: agent instructions, worker definitions, and MCP configs.

Ports Write-GeneratedCLAUDEmd, Write-GeneratedWorkerAgent, Write-ClaudeConfig and the
.mcp.json writers.

Two behaviours matter most here:

* **Scheduler roles get no instruction file.** There is no wrapper LLM session to read it,
  and the Claude spike proved a stray CLAUDE.md *does* reach a one-shot worker, so writing
  one would actively leak wrapper-loop instructions into worker context.
* **Worker definitions carry the KILN-STATUS contract**, taken from `status_contract.py` so
  the instruction the worker sees and the parser the scheduler runs cannot drift apart.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Profile, RoleConfig
from .paths import KilnPaths
from .templates import (
    generated_header,
    join_blocks,
    read_constitution,
    read_constitution_header,
    read_role,
    read_template,
)

# scheduler/ is a sibling package under kiln/framework.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scheduler.routing import load_routing_table
from scheduler.status_contract import WORKER_STATUS_INSTRUCTION

#: The interpreter `.mcp.json` names for kiln-channel. Deliberately the bare command rather
#: than sys.executable: the agent CLI resolves it from PATH at spawn time, which may not be
#: the interpreter running the launcher. `cli.warn_if_channel_unavailable` probes this exact
#: command so the preflight check tests what will actually run.
MCP_PYTHON = "python"

#: What channel.py imports, mirroring its mcp 1.x/2.x compatibility fallback.
CHANNEL_IMPORT_PROBE = (
    "try:\n"
    "    from mcp.server.fastmcp import FastMCP\n"
    "except ImportError:\n"
    "    from mcp.server.mcpserver import MCPServer\n"
)

#: Backends with real in-session worker delegation, so their wrapper gets the delegating
#: prompt rather than the role's own work rules.
DELEGATING_AGENTS = ("claude", "copilot", "codex")

DEFAULT_HANDOFF_TARGET = "specifier"

COMMIT_FORMATS = {
    "specifier": "[Specifier] <feature name> - <what was specified>",
    "coder": "[Coder] <feature name> - TDD implementation of <what>",
    "refactorer": "[Refactorer] <feature name> - <quality gate results>",
    "architect": "[Architect] <feature name> - <structural changes made>",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def instruction_file_for(role: RoleConfig, worktree: Path) -> Path:
    """Where each backend looks for project instructions."""
    if role.agent == "copilot":
        return worktree / ".github" / "copilot-instructions.md"
    if role.agent == "codex":
        return worktree / "AGENTS.md"
    return worktree / "CLAUDE.md"


def build_substitutions(
    role: RoleConfig, paths: KilnPaths, branch: str, worktree: Path
) -> dict[str, str]:
    routing = load_routing_table(paths.workflow_md)
    target = routing.resolve(role.role) or DEFAULT_HANDOFF_TARGET
    return {
        "{{ROLE}}": role.role,
        "{{ROLE_UPPER}}": role.role.upper(),
        "{{MODE}}": role.mode,
        "{{BRANCH}}": branch,
        "{{DB_PATH}}": str(paths.db_path),
        "{{WORKTREE}}": worktree.name,
        "{{HANDOFF_TARGET}}": target,
        "{{COMMIT_FORMAT}}": COMMIT_FORMATS.get(role.role, f"[{role.role}] <description>"),
    }


def render_instructions(
    role: RoleConfig, paths: KilnPaths, branch: str, worktree: Path, profile: Profile | None = None
) -> str:
    """
    Assemble a wrapper role's instruction file.

    Auto-mode delegating roles get the wrapper prompt + loop + runtime + workflow only: the
    role's own work rules and the engineering/project constitution live in the worker
    definition instead, because the wrapper never does implementation work itself.

    `profile` is optional (defaults to "no inbox") only so callers that do not care about
    inbox-aware loops -- most tests -- do not have to construct one; the real launcher
    always passes it.
    """
    delegates = role.agent in DELEGATING_AGENTS
    has_inbox = bool(profile and profile.inbox_watches(role.role))
    loop_name = f"loop-{role.mode}-{role.agent}"
    if has_inbox:
        # A companion `inbox` pane already receives and merges for this role (see
        # roles/human-in-the-loop.md -> "Receiving Messages"). The plain loop template's
        # Step 1/5 "receive" cycle assumes this role does its own polling and merging --
        # if used here anyway, either the session blocks forever racing the inbox for the
        # same message, or (observed live) it silently skips its own receive step and,
        # with it, the `set-status.py waiting` call that step starts with, leaving the tab
        # title stuck on whatever state it last reported (e.g. "handoff") forever. The
        # `-with-inbox` variant replaces that whole cycle with an explicit, mandatory
        # "waiting" status reset right after sending a handoff.
        loop_name += "-with-inbox"
    loop = read_template(paths, loop_name)
    runtime = read_template(paths, f"runtime-{role.agent}")
    workflow = read_constitution(paths, "workflow", required=True)

    if role.mode == "auto" and delegates:
        blocks = [
            read_template(paths, f"wrapper-prompt-auto-{role.agent}"),
            loop,
            runtime,
            workflow,
        ]
    else:
        blocks = [
            read_role(paths, role.role),
            loop,
            runtime,
            read_constitution_header(paths),
            read_constitution(paths, "project"),
            read_constitution(paths, "engineering"),
            workflow,
        ]

    substitutions = build_substitutions(role, paths, branch, worktree)
    return generated_header(_now()) + join_blocks(blocks, substitutions)


def write_instructions(
    role: RoleConfig, paths: KilnPaths, branch: str, worktree: Path, profile: Profile | None = None
) -> Path | None:
    """
    Write a role's instruction file, or remove it for scheduler roles.

    Removal matters: switching a role from the wrapper to the scheduler would otherwise
    leave the previous run's CLAUDE.md in the worktree, and the Claude spike confirmed a
    stray CLAUDE.md *is* read by one-shot workers — so a stale file would silently feed
    wrapper-loop instructions into worker context.

    Returns the path written, or None when the role is scheduler-driven.
    """
    if role.is_passive:
        # A passive pane (inbox, dashboard) has no worktree and no generated files of its
        # own (RoleConfig.is_passive) -- it shares its worktree (@current) with a real role,
        # e.g. an inbox with the human-in-the-loop it watches in the scheduler-all profile.
        # instruction_file_for() would resolve to *that* role's CLAUDE.md, so deleting "a
        # stale file for this role" here deletes a real, just-written file instead: the real
        # role is processed first in profile.roles and writes CLAUDE.md correctly, then the
        # passive pane is processed right after and (observed live, for inbox specifically)
        # silently deletes the same path, leaving the real role's session with no
        # instructions at all -- it never learns to call set-status.py, so its tab-bar badge
        # sticks on whatever state it last managed to report.
        return None

    if role.uses_scheduler:
        stale = instruction_file_for(role, worktree)
        if stale.is_file():
            stale.unlink()
        return None

    target = instruction_file_for(role, worktree)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_instructions(role, paths, branch, worktree, profile), encoding="utf-8"
    )
    return target


# --- worker agent definitions --------------------------------------------------------

@dataclass(frozen=True)
class WorkerFile:
    path: Path
    content: str


def _worker_description(role: str) -> str:
    # No raw ':' — an unquoted colon breaks Copilot's YAML frontmatter parsing.
    return (
        f"Performs the {role} role's implementation work for one handoff cycle. "
        f"Dispatched by the {role} wrapper or scheduler; not for direct standalone use."
    )


def render_worker_body(role: RoleConfig, paths: KilnPaths) -> str:
    """
    The worker's actual instructions: role rules plus project/engineering constitution.

    Deliberately excludes workflow.md — handoff and messaging protocol belong to whatever
    dispatched the worker, never to the worker itself.
    """
    blocks = [
        read_role(paths, role.role),
        read_constitution(paths, "project"),
        read_constitution(paths, "engineering"),
    ]
    body = join_blocks(blocks, {"{{ROLE}}": role.role, "{{ROLE_UPPER}}": role.role.upper()})
    # Single source of truth with the parser that reads the worker's output.
    return f"{body}\n\n---\n\n{WORKER_STATUS_INSTRUCTION.strip()}\n"


def render_worker_file(role: RoleConfig, paths: KilnPaths) -> WorkerFile:
    """Render the worker definition in the format its backend discovers."""
    body = render_worker_body(role, paths)
    description = _worker_description(role.role)
    model = role.worker_model or role.model
    timestamp = _now()
    path = paths.worker_agent_file(role.role, role.agent)

    if role.agent == "codex":
        # A TOML literal string ('''...''') so backticks, quotes and Windows backslashes in
        # the constitution need no escaping.
        content = (
            f"# Auto-generated by kiln on {timestamp}\n"
            "# DO NOT EDIT MANUALLY\n\n"
            f'name = "{role.role}-worker"\n'
            f'description = "{description}"\n'
            "mcp_servers = {}\n"
            "developer_instructions = '''\n"
            f"{body}\n"
            "'''\n"
        )
        return WorkerFile(path=path, content=content)

    frontmatter = ["---", f"name: {role.role}-worker", f"description: {description}"]
    if role.agent == "copilot":
        # A strict allowlist with no MCP server name, mirroring the Claude worker's isolation.
        frontmatter += ["tools:", "  - read", "  - write", "  - shell"]
    else:
        frontmatter.append(
            "tools: Read, Write, Edit, Glob, Grep, Bash, PowerShell, Skill, "
            "NotebookEdit, TodoWrite"
        )
        if model:
            frontmatter.append(f"model: {model}")
    frontmatter.append("---")

    content = "\n".join(frontmatter) + "\n\n" + generated_header(timestamp) + body
    return WorkerFile(path=path, content=content)


def write_worker_file(role: RoleConfig, paths: KilnPaths) -> Path | None:
    """Write the role's worker definition. None for a passive pane, which delegates to nothing."""
    if role.is_passive:
        return None
    rendered = render_worker_file(role, paths)
    rendered.path.parent.mkdir(parents=True, exist_ok=True)
    rendered.path.write_text(rendered.content, encoding="utf-8")
    return rendered.path


# --- MCP configuration ----------------------------------------------------------------

def build_mcp_config(
    paths: KilnPaths, role: str | None, branch: str, include_channel: bool
) -> dict:
    """
    The .mcp.json contents for a worktree (or the project root).

    kiln-channel is only included when a role owns this directory: it is role-scoped by
    env var, so a shared config with the wrong role would deliver another role's messages.
    """
    config: dict = {
        "mcpServers": {
            "kiln-db": {
                "command": "npx",
                "args": ["mcp-sqlite", str(paths.db_path)],
            }
        }
    }
    if include_channel and role:
        config["mcpServers"]["kiln-channel"] = {
            "command": MCP_PYTHON,
            "args": [str(paths.channel_script)],
            "env": {
                "KILN_ROLE": role,
                "KILN_DB_PATH": str(paths.db_path),
                "KILN_BRANCH": branch,
                "KILN_CHANNEL_LOG": str(paths.channel_log(role)),
            },
        }
    return config


def write_mcp_config(
    target_dir: Path, paths: KilnPaths, role: str | None, branch: str, include_channel: bool
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / ".mcp.json"
    config = build_mcp_config(paths, role, branch, include_channel)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def build_copilot_mcp_config(paths: KilnPaths) -> dict:
    """Copilot reads one shared global config rather than a per-worktree file."""
    return {
        "mcpServers": {
            "kiln-db": {"command": "npx", "args": ["mcp-sqlite", str(paths.db_path)]}
        }
    }


def build_codex_config_toml(paths: KilnPaths, worktree: Path) -> str:
    """
    A role's isolated CODEX_HOME config.

    The trust entry is required: `--dangerously-bypass-approvals-and-sandbox` alone still
    prompts, because Codex gates that flag behind per-project trust, and a fresh isolated
    CODEX_HOME has no trust record for the role's worktree.
    """
    return (
        f"[projects.'{worktree}']\n"
        'trust_level = "trusted"\n\n'
        "[mcp_servers.kiln-db]\n"
        'command = "npx"\n'
        f'args = ["mcp-sqlite", "{str(paths.db_path).replace(chr(92), chr(92) * 2)}"]\n'
    )


def generate_all(profile: Profile, paths: KilnPaths, branch: str) -> dict[str, list[Path]]:
    """Write every generated file for a profile. Returns what was written, by category."""
    written: dict[str, list[Path]] = {"instructions": [], "workers": []}
    for role in profile.roles:
        worktree = (
            paths.project_root if role.uses_current_dir else paths.worktree_path(role.worktree)
        )
        instructions = write_instructions(role, paths, branch, worktree)
        if instructions:
            written["instructions"].append(instructions)
        written["workers"].append(write_worker_file(role, paths))
    return written
