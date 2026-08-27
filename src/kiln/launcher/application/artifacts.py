"""
Generated per-role files: agent instructions, worker definitions, and MCP configs.

Ports Write-GeneratedCLAUDEmd, Write-GeneratedWorkerAgent, Write-ClaudeConfig and the
.mcp.json writers.

Two behaviours matter most here:

* **Scheduler roles get no instruction file.** There is no interactive session to read it,
  and the Claude spike proved a stray CLAUDE.md *does* reach a one-shot worker, so writing
  one would actively leak loop instructions into worker context. Autonomous work always runs
  on the scheduler, so instruction files belong to manual roles alone.
* **Worker definitions carry the KILN-STATUS contract**, taken from `status_contract.py` so
  the instruction the worker sees and the parser the scheduler runs cannot drift apart.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..domain.paths import KilnPaths, python_command
from ..domain.profile import Profile, RoleConfig
from .templates import (
    generated_header,
    join_blocks,
    read_constitution,
    read_constitution_header,
    read_role,
    read_template,
)

# scheduler/ is a sibling package under src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.scheduler.domain.routing import load_routing_table, render_routing_table
from kiln.scheduler.domain.status_contract import WORKER_STATUS_INSTRUCTION

#: Bare interpreter resolved by the agent CLI when it starts the MCP server.
MCP_PYTHON = python_command()

#: What channel.py imports, mirroring its mcp 1.x/2.x compatibility fallback.
CHANNEL_IMPORT_PROBE = (
    "try:\n"
    "    from mcp.server.fastmcp import FastMCP\n"
    "except ImportError:\n"
    "    from mcp.server.mcpserver import MCPServer\n"
)

#: Backends verified to tolerate a blocking `wait_for_message()` MCP call.
BLOCKING_CHANNEL_AGENTS = frozenset({"claude"})

DEFAULT_HANDOFF_TARGET = "specifier"

KNOWLEDGE_INSTRUCTION = """## Project Knowledge

When domain, product, architectural, operational, or external constraints matter, search the
approved project knowledge with `kiln knowledge search "<query>"`. Use
`kiln knowledge show <document-id>` for the complete indexed document. Cite the returned path,
heading, or PDF page in decisions and handoffs. Knowledge is supporting evidence; if it
conflicts with the constitution, surface the conflict instead of overriding the constitution.
Autonomous roles must not add or remove knowledge sources.
"""

COMMIT_FORMATS = {
    "specifier": "[Specifier] <feature name> - <what was specified>",
    "coder": "[Coder] <feature name> - TDD implementation of <what>",
    "refactorer": "[Refactorer] <feature name> - <quality gate results>",
    "architect": "[Architect] <feature name> - <structural changes made>",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def instruction_file_for(role: RoleConfig, worktree: Path) -> Path:
    """
    Where each backend looks for project instructions.

    `grok` has no filename of its own. Verified against grok 1.0.5 with `grok inspect`: it
    discovers `AGENTS.md` and `CLAUDE.md` from the repo root and ignores `GROK.md` and
    `.grok/GROK.md` entirely — so one of the other two conventions has to be reused, and
    AGENTS.md is both the cross-vendor name and the one grok lists first. Sharing it with
    Codex costs nothing: a worktree belongs to exactly one role, so no directory is ever
    written by two backends. It is already in REQUIRED_GITIGNORE_ENTRIES for the same reason.
    """
    if role.agent == "copilot":
        return worktree / ".github" / "copilot-instructions.md"
    if role.agent in ("codex", "grok", "pi"):
        return worktree / "AGENTS.md"
    return worktree / "CLAUDE.md"


def channel_is_available(role: RoleConfig | None) -> bool:
    """
    Whether this role's `.mcp.json` should register `kiln-channel` at all.

    One rule for the two places that write such a file — `workspace.prepare_worktrees` for a
    worktree role and `cli` for whichever role owns the project root — because they were
    deciding it separately and had already drifted: the root copy asked only "is there a role
    here", so an `@current` role got a blocking channel regardless of whether its loop blocks
    or whether it runs under the scheduler at all.

    Three conditions, each for a failure the entry causes rather than prevents. No role: the
    server is role-scoped by env var, so a shared config would deliver someone else's
    messages. A scheduler role: it polls in Python and its worker runs with
    `--strict-mcp-config`, so a channel implies it still receives its own handoffs. A backend
    outside `BLOCKING_CHANNEL_AGENTS`: its loop is told to poll `kiln-db` and never to call
    `wait_for_message()`, so registering the tool contradicts its own instructions.
    """
    return role is not None and not role.uses_scheduler and role.agent in BLOCKING_CHANNEL_AGENTS


def build_substitutions(
    role: RoleConfig,
    paths: KilnPaths,
    branch: str,
    worktree: Path,
    profile: Profile | None = None,
) -> dict[str, str]:
    routing = (
        profile.routing
        if profile is not None and profile.routing.rules
        else load_routing_table(paths.workflow_md)
    )
    target = routing.resolve(role.role) or DEFAULT_HANDOFF_TARGET
    return {
        "{{ROUTING_TABLE}}": render_routing_table(routing),
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

    Only manual roles reach this: autonomous work runs on the deterministic scheduler, and the
    loader rejects `mode: auto` without one. The loop template is therefore always the manual
    variant.

    `profile` is optional (defaults to "no inbox") only so callers that do not care about
    inbox-aware loops -- most tests -- do not have to construct one; the real launcher
    always passes it.
    """
    has_inbox = bool(profile and profile.inbox_watches(role.role))
    loop_name = f"loop-manual-{role.agent}"
    if has_inbox:
        # The companion inbox owns receiving; this variant only resets the wrapper to waiting.
        loop_name += "-with-inbox"
    loop = read_template(paths, loop_name)
    runtime = read_template(paths, f"runtime-{role.agent}")
    workflow = read_constitution(paths, "workflow", required=True)

    blocks = [
        read_role(paths, role.role),
        loop,
        runtime,
        read_constitution_header(paths),
        read_constitution(paths, "project"),
        read_constitution(paths, "engineering"),
        KNOWLEDGE_INSTRUCTION,
        workflow,
    ]

    substitutions = build_substitutions(role, paths, branch, worktree, profile)
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
        # Passive panes share another role's worktree and must not alter its generated files.
        return None

    if role.uses_scheduler:
        stale = instruction_file_for(role, worktree)
        if stale.is_file():
            stale.unlink()
        return None

    target = instruction_file_for(role, worktree)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_instructions(role, paths, branch, worktree, profile), encoding="utf-8")
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
        KNOWLEDGE_INSTRUCTION,
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
        return WorkerFile(
            path=path, content=_codex_worker_content(role, description, timestamp, body)
        )

    frontmatter = _worker_frontmatter(role, description, model)
    content = "\n".join(frontmatter) + "\n\n" + generated_header(timestamp) + body
    return WorkerFile(path=path, content=content)


def _codex_worker_content(role, description: str, timestamp: str, body: str) -> str:
    return (
        f"# Auto-generated by kiln on {timestamp}\n"
        "# DO NOT EDIT MANUALLY\n\n"
        f'name = "{role.role}-worker"\n'
        f'description = "{description}"\n'
        "mcp_servers = {}\n"
        "developer_instructions = '''\n"
        f"{body}\n"
        "'''\n"
    )


def _worker_frontmatter(role: RoleConfig, description: str, model: str) -> list[str]:
    frontmatter = ["---", f"name: {role.role}-worker", f"description: {description}"]
    if role.agent == "copilot":
        # A strict allowlist with no MCP server name, mirroring the Claude worker's isolation.
        frontmatter += ["tools:", "  - read", "  - write", "  - shell"]
    elif role.agent in ("grok", "pi"):
        # No `tools:` line: those are Claude's own built-in tool names (below), and would be
        # actively wrong content in a grok file -- grok's scheduler adapter feeds this file's
        # description+prompt through an inline --agents payload and never reads `.tools`.
        if model:
            frontmatter.append(f"model: {model}")
    else:
        frontmatter.append(
            "tools: Read, Write, Edit, Glob, Grep, Bash, PowerShell, Skill, NotebookEdit, TodoWrite"
        )
        if model:
            frontmatter.append(f"model: {model}")
    frontmatter.append("---")
    return frontmatter


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
        "mcpServers": {"kiln-db": {"command": "npx", "args": ["mcp-sqlite", str(paths.db_path)]}}
    }


#: Allows cold `npx mcp-sqlite` startup before Codex abandons the MCP server.
CODEX_MCP_STARTUP_TIMEOUT_SEC = 120


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
        f"startup_timeout_sec = {CODEX_MCP_STARTUP_TIMEOUT_SEC}\n"
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
