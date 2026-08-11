"""
Builds the one-shot worker invocation's context from the already-generated worker agent
file.

`Write-GeneratedWorkerAgent` (bin/kiln.ps1:872) already produces exactly the content a
worker needs — role rules plus the project/engineering constitution — as a frontmatter
markdown file. This module reads that file rather than regenerating it, so the scheduler
path and the legacy in-session delegation path cannot describe the worker differently.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .status_contract import WORKER_STATUS_INSTRUCTION

_FRONTMATTER_FENCE = "---"


@dataclass(frozen=True)
class WorkerDefinition:
    """A parsed `.claude/agents/<role>-worker.md` file."""

    name: str
    description: str
    prompt: str
    model: str | None = None
    tools: str | None = None


def parse_worker_definition(text: str) -> WorkerDefinition:
    """
    Split a generated worker agent file into frontmatter fields and body.

    Frontmatter here is always machine-written by kiln.ps1/kiln.sh with flat `key: value`
    pairs, so this deliberately does not pull in a YAML dependency; it splits on the first
    colon only, which keeps descriptions containing colons intact.
    """
    lines = text.lstrip().splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ValueError("worker definition is missing its opening '---' frontmatter fence")

    fields: dict[str, str] = {}
    body_start = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_FENCE:
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()

    if body_start is None:
        raise ValueError("worker definition is missing its closing '---' frontmatter fence")

    name = fields.get("name", "")
    if not name:
        raise ValueError("worker definition frontmatter has no 'name'")

    return WorkerDefinition(
        name=name,
        description=fields.get("description", ""),
        prompt="\n".join(lines[body_start:]).strip(),
        model=fields.get("model") or None,
        tools=fields.get("tools") or None,
    )


def parse_toml_worker_definition(text: str) -> WorkerDefinition:
    """
    Parse a generated `.codex/agents/<role>-worker.toml` file.

    Structurally nothing like Claude/Copilot's frontmatter-markdown shape (see
    `render_worker_file`'s `codex` branch in `launcher/generate.py`): a flat TOML document
    with `name`, `description`, `mcp_servers = {}`, and a `developer_instructions` literal
    string holding the *entire* worker body (role rules + constitution + status
    instruction). That field maps to `.prompt` -- after this, `WorkerDefinition.prompt` is
    the canonical worker persona text regardless of which backend produced the file, so
    nothing downstream (scheduler retry/delegate logic) needs to know the format differed.
    Codex's TOML never sets `model`/`tools`, so both stay `None`.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"worker definition is not valid TOML: {exc}") from exc

    name = data.get("name", "")
    if not name:
        raise ValueError("worker definition TOML has no 'name'")

    return WorkerDefinition(
        name=name,
        description=data.get("description", ""),
        prompt=str(data.get("developer_instructions", "")).strip(),
    )


def load_worker_definition(path: str | Path) -> WorkerDefinition:
    """Read and parse a generated worker agent file, dispatching on its format by extension."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"no generated worker agent at {file_path}")
    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix == ".toml":
        return parse_toml_worker_definition(text)
    return parse_worker_definition(text)


def build_agents_payload(definition: WorkerDefinition) -> str:
    """
    Render the `--agents` JSON that defines this worker for a one-shot Claude run.

    Verified live: the agent's prompt governs the response, which is what lets the
    scheduler hand a worker its role without an interactive session to delegate through.
    """
    return json.dumps(
        {
            definition.name: {
                "description": definition.description,
                "prompt": definition.prompt,
            }
        }
    )


def build_task_prompt(
    *,
    handoff_text: str,
    role: str,
    branch: str,
    worktree: str | Path,
    retry_of: str | None = None,
) -> str:
    """
    Assemble the task prompt for one worker invocation.

    The worker is told its workspace and given the inbound handoff verbatim — never a
    summary — so nothing the sender wrote is lost in translation. The status instruction is
    appended from status_contract so the parser and the instruction cannot drift.

    `retry_of` carries the previous attempt's failure report into a second attempt, which
    is what makes the retry meaningfully different from a re-run.
    """
    sections = [
        f"You are the {role} worker for one Kiln handoff cycle.",
        f"Workspace: {worktree}\nBranch: {branch}",
        "## Inbound handoff\n\n" + handoff_text.strip(),
    ]

    if retry_of:
        sections.append(
            "## Previous attempt failed\n\n"
            "A previous attempt at this same task reported:\n\n"
            f"{retry_of.strip()}\n\n"
            "Address that failure directly rather than repeating the same approach."
        )

    sections.append(
        "## Your task\n\n"
        "Apply your own role rules to this state. Do not send handoffs, write to the "
        "message database, or spawn further agents — the scheduler owns all of that."
    )
    sections.append(WORKER_STATUS_INSTRUCTION.strip())
    return "\n\n".join(sections)
