"""
Generated per-role files.

The load-bearing assertions here are about *exclusion*: a scheduler role must get no
instruction file at all, and a worker must never receive the handoff protocol. Both are
things the spike showed leak into worker context if written.
"""

from __future__ import annotations

import json

import pytest
from launcher import generate
from launcher.config import RoleConfig, parse_profile
from launcher.paths import KilnPaths
from scheduler.status_contract import SENTINEL_PREFIX

CONSTITUTION = {
    "workflow.md": "# Workflow Rules\n\n## Handoff Routing\n\n| Role | Sends to |\n"
    "| ---- | -------- |\n| coder | refactorer |\n| specifier | coder |\n",
    "engineering.md": "# Engineering Rules\n\nWork in small increments.\n",
    "project.md": "# Project Rules\n\nPython, pytest.\n",
}

ROLES = {
    "coder.md": "# Coder\n\nImplement via TDD.\n\n## Message Loop\n\nWait for a message.\n",
    "specifier.md": "# Specifier\n\nWrite specs.\n",
}

TEMPLATES = {
    "loop-auto-claude.md": "# Loop\n\nRole is {{ROLE}}, target {{HANDOFF_TARGET}}.\n",
    "loop-manual-claude.md": "# Manual Loop\n\nRole {{ROLE}}.\n",
    "runtime-claude.md": "# Runtime\n\nBranch {{BRANCH}}, db {{DB_PATH}}.\n",
    "wrapper-prompt-auto-claude.md": "# Wrapper\n\nDelegate to {{ROLE}}-worker.\n",
    "loop-auto-copilot.md": "# Copilot Loop\n",
    "runtime-copilot.md": "# Copilot Runtime\n",
    "wrapper-prompt-auto-copilot.md": "# Copilot Wrapper\n",
    "loop-auto-codex.md": "# Codex Loop\n",
    "runtime-codex.md": "# Codex Runtime\n",
    "wrapper-prompt-auto-codex.md": "# Codex Wrapper\n",
}


@pytest.fixture
def paths(tmp_path):
    project = tmp_path / "proj"
    framework = tmp_path / "fw"

    constitution = project / "kiln" / "project" / "constitution"
    constitution.mkdir(parents=True)
    for name, body in CONSTITUTION.items():
        (constitution / name).write_text(body, encoding="utf-8")
    (project / "kiln" / "project" / "constitution.md").write_text(
        "# Constitution\n\nPreamble.\n", encoding="utf-8"
    )

    roles = project / "kiln" / "project" / "roles"
    roles.mkdir(parents=True)
    for name, body in ROLES.items():
        (roles / name).write_text(body, encoding="utf-8")

    templates = framework / "kiln" / "framework" / "templates"
    templates.mkdir(parents=True)
    for name, body in TEMPLATES.items():
        (templates / name).write_text(body, encoding="utf-8")

    return KilnPaths.create(project, framework)


def role(**kwargs):
    kwargs.setdefault("role", "coder")
    return RoleConfig(**kwargs)


class TestInstructionFiles:
    def test_scheduler_role_gets_no_instruction_file(self, paths):
        # There is no wrapper session to read it, and the spike proved a stray CLAUDE.md
        # does reach one-shot workers.
        written = generate.write_instructions(
            role(scheduler="python", mode="auto"), paths, "main", paths.project_root
        )
        assert written is None
        assert not (paths.project_root / "CLAUDE.md").exists()

    def test_removes_a_stale_instruction_file_when_switching_to_the_scheduler(self, paths):
        # Switching a role from wrapper to scheduler must delete the old CLAUDE.md: the
        # spike proved a stray one is read by one-shot workers, so leaving it would leak
        # wrapper-loop instructions into worker context.
        generate.write_instructions(role(), paths, "main", paths.project_root)
        assert (paths.project_root / "CLAUDE.md").exists()

        generate.write_instructions(
            role(scheduler="python", mode="auto"), paths, "main", paths.project_root
        )
        assert not (paths.project_root / "CLAUDE.md").exists()

    def test_wrapper_role_gets_claude_md(self, paths):
        written = generate.write_instructions(role(), paths, "main", paths.project_root)
        assert written.name == "CLAUDE.md"
        assert written.read_text(encoding="utf-8").startswith("<!-- Auto-generated")

    @pytest.mark.parametrize(
        ("agent", "expected"),
        [
            ("claude", "CLAUDE.md"),
            ("codex", "AGENTS.md"),
            ("copilot", "copilot-instructions.md"),
        ],
    )
    def test_each_backend_gets_its_own_filename(self, paths, agent, expected):
        written = generate.write_instructions(
            role(agent=agent), paths, "main", paths.project_root
        )
        assert written.name == expected

    def test_auto_role_gets_the_wrapper_prompt_not_its_own_role_rules(self, paths):
        # The wrapper delegates; the role's work rules belong to the worker.
        content = generate.render_instructions(role(), paths, "main", paths.project_root)
        assert "# Wrapper" in content
        assert "Implement via TDD" not in content

    def test_manual_role_gets_its_role_rules_and_full_constitution(self, paths):
        content = generate.render_instructions(
            role(role="specifier", mode="manual"), paths, "main", paths.project_root
        )
        assert "Write specs" in content
        assert "# Engineering Rules" in content
        assert "Preamble" in content

    def test_placeholders_are_substituted(self, paths):
        content = generate.render_instructions(role(), paths, "main", paths.project_root)
        assert "{{ROLE}}" not in content
        assert "Role is coder" in content
        assert "Branch main" in content

    def test_handoff_target_comes_from_the_routing_table(self, paths):
        content = generate.render_instructions(role(), paths, "main", paths.project_root)
        assert "target refactorer" in content

    def test_unknown_role_falls_back_to_specifier(self, paths):
        content = generate.render_instructions(
            role(role="nobody"), paths, "main", paths.project_root
        )
        assert "target specifier" in content

    def test_role_loop_sections_are_stripped(self, paths):
        content = generate.render_instructions(
            role(role="coder", mode="manual"), paths, "main", paths.project_root
        )
        # The loop belongs to the wrapper template, not the role file's own copy.
        assert "Wait for a message" not in content


class TestWorkerFiles:
    def test_carries_the_status_contract(self, paths):
        body = generate.render_worker_body(role(), paths)
        assert SENTINEL_PREFIX in body

    def test_includes_role_and_constitution(self, paths):
        body = generate.render_worker_body(role(), paths)
        assert "Implement via TDD" in body
        assert "# Engineering Rules" in body
        assert "# Project Rules" in body

    def test_excludes_the_handoff_protocol(self, paths):
        # Messaging is the dispatcher's job; a worker that sends its own handoff would
        # duplicate what the scheduler does.
        assert "Handoff Routing" not in generate.render_worker_body(role(), paths)

    def test_claude_worker_is_frontmatter_markdown(self, paths):
        rendered = generate.render_worker_file(role(model="claude-sonnet-5"), paths)
        assert rendered.path.name == "coder-worker.md"
        assert rendered.content.startswith("---\nname: coder-worker\n")
        assert "model: claude-sonnet-5" in rendered.content

    def test_worker_model_overrides_the_wrapper_model(self, paths):
        rendered = generate.render_worker_file(
            role(model="opus", worker_model="sonnet"), paths
        )
        assert "model: sonnet" in rendered.content

    def test_copilot_worker_uses_a_strict_tool_allowlist(self, paths):
        rendered = generate.render_worker_file(role(agent="copilot"), paths)
        assert rendered.path.name == "coder-worker.agent.md"
        assert "  - read" in rendered.content
        assert "kiln-db" not in rendered.content

    def test_codex_worker_is_toml_with_no_mcp_servers(self, paths):
        rendered = generate.render_worker_file(role(agent="codex"), paths)
        assert rendered.path.name == "coder-worker.toml"
        assert "mcp_servers = {}" in rendered.content
        assert "developer_instructions = '''" in rendered.content

    def test_description_contains_no_bare_colon(self, paths):
        # An unquoted ':' breaks Copilot's YAML frontmatter parsing.
        rendered = generate.render_worker_file(role(agent="copilot"), paths)
        description = next(
            line for line in rendered.content.splitlines() if line.startswith("description:")
        )
        assert ":" not in description[len("description:") :]

    def test_scheduler_roles_still_get_a_worker_file(self, paths):
        # The scheduler reads this exact file to build its --agents payload.
        rendered = generate.render_worker_file(role(scheduler="python"), paths)
        assert rendered.path.name == "coder-worker.md"

    def test_written_worker_parses_back(self, paths):
        from scheduler.worker_prompt import load_worker_definition

        path = generate.write_worker_file(role(), paths)
        definition = load_worker_definition(path)
        assert definition.name == "coder-worker"
        assert SENTINEL_PREFIX in definition.prompt


class TestMcpConfig:
    def test_root_config_includes_the_channel_for_its_role(self, paths):
        config = generate.build_mcp_config(paths, "specifier", "main", include_channel=True)
        assert set(config["mcpServers"]) == {"kiln-db", "kiln-channel"}
        assert config["mcpServers"]["kiln-channel"]["env"]["KILN_ROLE"] == "specifier"
        assert config["mcpServers"]["kiln-channel"]["env"]["KILN_BRANCH"] == "main"

    def test_config_without_a_role_has_db_only(self, paths):
        config = generate.build_mcp_config(paths, None, "main", include_channel=True)
        assert set(config["mcpServers"]) == {"kiln-db"}

    def test_channel_can_be_excluded(self, paths):
        config = generate.build_mcp_config(paths, "coder", "main", include_channel=False)
        assert "kiln-channel" not in config["mcpServers"]

    def test_written_config_is_valid_json(self, paths, tmp_path):
        path = generate.write_mcp_config(tmp_path / "wt", paths, "coder", "main", True)
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kiln-db"]

    def test_codex_config_seeds_project_trust(self, paths):
        # Without this, the bypass flag still prompts and a headless run would hang.
        toml = generate.build_codex_config_toml(paths, paths.project_root)
        assert 'trust_level = "trusted"' in toml
        assert str(paths.project_root) in toml


class TestGenerateAll:
    def test_writes_workers_for_every_role_and_skips_scheduler_instructions(self, paths):
        config = {
            "profiles": {
                "p": {
                    "terminals": [
                        {"role": "specifier", "worktree": "@current", "mode": "manual"},
                        {
                            "role": "coder",
                            "worktree": "@current",
                            "mode": "auto",
                            "scheduler": "python",
                        },
                    ]
                }
            }
        }
        profile = parse_profile(config, "p")

        written = generate.generate_all(profile, paths, "main")

        assert len(written["workers"]) == 2
        assert len(written["instructions"]) == 1
        assert written["instructions"][0].name == "CLAUDE.md"
