"""
Pane command construction.

This is the one seam between the tested Python core and a real terminal: whatever these
produce is typed verbatim into a shell. A quoting mistake here is invisible until a pane
silently runs the wrong thing, so the renderers are tested against paths with spaces and
quotes rather than only tidy ones.
"""

from __future__ import annotations

import pytest
from launcher.commands import (
    START_PROMPT,
    AgentCommand,
    build_agent_command,
    render_posix,
    render_powershell,
)
from launcher.config import RoleConfig
from launcher.paths import KilnPaths


@pytest.fixture
def paths(tmp_path):
    return KilnPaths.create(tmp_path / "proj", tmp_path / "fw")


def build(paths, **role_kwargs):
    role_kwargs.setdefault("role", "coder")
    return build_agent_command(RoleConfig(**role_kwargs), paths, branch="main")


class TestClaude:
    def test_uses_the_configured_model(self, paths):
        assert "claude-sonnet-5" in build(paths, model="claude-sonnet-5").argv

    def test_falls_back_to_a_default_model(self, paths):
        argv = build(paths).argv
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_auto_mode_bypasses_permissions(self, paths):
        argv = build(paths, mode="auto").argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    def test_manual_mode_keeps_prompts(self, paths):
        # A manual role is human-supervised; silently bypassing approvals would be wrong.
        argv = build(paths, mode="manual").argv
        assert argv[argv.index("--permission-mode") + 1] == "default"

    def test_wires_the_project_mcp_config(self, paths):
        assert "./.mcp.json" in build(paths).argv

    def test_ends_with_the_start_prompt(self, paths):
        assert build(paths).argv[-1] == START_PROMPT

    def test_sets_a_display_name(self, paths):
        argv = build(paths, role="human-in-the-loop").argv
        assert argv[argv.index("-n") + 1] == "Human In The Loop"


class TestCopilot:
    def test_allows_all_and_passes_the_prompt(self, paths):
        argv = build(paths, agent="copilot").argv
        assert argv[0] == "copilot"
        assert "--allow-all" in argv
        assert argv[-1] == START_PROMPT

    def test_model_is_optional(self, paths):
        assert "--model" not in build(paths, agent="copilot").argv
        assert "--model" in build(paths, agent="copilot", model="gpt-5").argv

    def test_gets_a_banner_because_its_cli_shows_no_role(self, paths):
        assert build(paths, agent="copilot").banner == "Coder"


class TestCodex:
    def test_isolates_config_via_codex_home(self, paths):
        command = build(paths, agent="codex")
        assert "CODEX_HOME" in command.env
        assert command.env["CODEX_HOME"].endswith("codex-home\\coder") or command.env[
            "CODEX_HOME"
        ].endswith("codex-home/coder")

    def test_bypasses_approvals(self, paths):
        assert "--dangerously-bypass-approvals-and-sandbox" in build(paths, agent="codex").argv


class TestUnsupportedAgent:
    def test_reports_in_the_pane_instead_of_failing_the_launch(self, paths):
        argv = build(paths, agent="grok").argv
        assert argv[0] == "echo"
        assert "not supported" in argv[1]


class TestScheduler:
    def _scheduler(self, paths, **kwargs):
        kwargs.setdefault("scheduler", "python")
        kwargs.setdefault("mode", "auto")
        return build(paths, **kwargs)

    def test_launches_the_module_not_the_script_path(self, paths):
        # A bare script path fails: the package uses relative imports.
        argv = self._scheduler(paths).argv
        assert argv[:3] == ["python", "-m", "scheduler.role_scheduler"]

    def test_sets_pythonpath_so_the_package_resolves(self, paths):
        env = self._scheduler(paths).env
        assert env["PYTHONPATH"].replace("\\", "/").endswith("kiln/framework")

    def test_passes_every_required_scheduler_argument(self, paths):
        argv = self._scheduler(paths).argv
        for flag in (
            "--role", "--branch", "--db-path", "--worktree", "--workflow", "--worker-agent"
        ):
            assert flag in argv, f"{flag} missing"
        assert argv[argv.index("--role") + 1] == "coder"
        assert argv[argv.index("--branch") + 1] == "main"

    def test_points_at_the_roles_own_worktree(self, paths):
        argv = self._scheduler(paths, worktree="coder").argv
        assert argv[argv.index("--worktree") + 1].endswith("coder")

    def test_current_dir_role_points_at_the_project_root(self, paths):
        argv = self._scheduler(paths, worktree="@current").argv
        assert argv[argv.index("--worktree") + 1] == str(paths.project_root)

    def test_prefers_the_worker_model(self, paths):
        argv = self._scheduler(paths, model="opus", worker_model="sonnet").argv
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_never_launches_the_agent_cli(self, paths):
        # `claude` still appears as the --agent VALUE (which adapter the scheduler uses),
        # but nothing invokes the CLI: the scheduler spawns workers itself, one shot each.
        command = self._scheduler(paths)
        assert command.argv[0] == "python"
        assert command.argv[command.argv.index("--agent") + 1] == "claude"

    def test_manual_role_still_gets_an_interactive_session(self, paths):
        # uses_scheduler is False for manual roles regardless of the flag.
        assert build(paths, scheduler="python", mode="manual").argv[0] == "claude"

    def test_role_without_the_flag_keeps_the_wrapper(self, paths):
        assert build(paths).argv[0] == "claude"

    def test_worker_debug_is_off_by_default(self, paths):
        assert "--worker-debug" not in self._scheduler(paths).argv

    def test_worker_debug_opts_in(self, paths):
        assert "--worker-debug" in self._scheduler(paths, worker_debug=True).argv


class TestInboxPane:
    """
    A notification pane, not an agent.

    It exists because the wrapper `human-in-the-loop` role could not both listen for
    handoffs and stay available to its human — see scheduler/inbox.py.
    """

    def _command(self, paths, **overrides):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig

        role = RoleConfig(
            role="inbox", worktree="@current", mode="manual",
            scheduler="inbox", watches="human-in-the-loop", **overrides
        )
        return build_agent_command(role, paths, "main")

    def test_runs_the_inbox_module(self, paths):
        argv = self._command(paths).argv
        assert argv[:3] == ["python", "-m", "scheduler.inbox"]

    def test_watches_the_role_it_was_told_to(self, paths):
        # Not its own name: the pane is 'inbox', the queue belongs to 'human-in-the-loop'.
        argv = self._command(paths).argv
        assert argv[argv.index("--role") + 1] == "human-in-the-loop"

    def test_falls_back_to_its_own_name_without_watches(self, paths):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig

        role = RoleConfig(role="human-in-the-loop", scheduler="inbox", mode="manual")
        argv = build_agent_command(role, paths, "main").argv
        assert argv[argv.index("--role") + 1] == "human-in-the-loop"

    def test_is_scoped_to_the_launch_branch(self, paths):
        # Messages are branch-scoped; the wrong branch looks like an empty inbox.
        argv = self._command(paths).argv
        assert argv[argv.index("--branch") + 1] == "main"

    def test_forces_utf8_so_the_glyphs_cannot_crash_it(self, paths):
        assert self._command(paths).env["PYTHONIOENCODING"] == "utf-8"

    def test_runs_no_agent_cli(self, paths):
        argv = self._command(paths).argv
        assert "claude" not in argv
        assert "--permission-mode" not in argv

    def test_writes_a_log_file(self, paths):
        argv = self._command(paths).argv
        assert argv[argv.index("--log-file") + 1].endswith("scheduler-inbox.log")


class TestDashboardPane:
    """A cross-role aggregate view, not an agent -- see scheduler/dashboard.py."""

    def _command(self, paths):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig

        role = RoleConfig(
            role="dashboard", worktree="@current", mode="manual", scheduler="dashboard"
        )
        return build_agent_command(role, paths, "main")

    def test_runs_the_dashboard_module(self, paths):
        argv = self._command(paths).argv
        assert argv[:3] == ["python", "-m", "scheduler.dashboard"]

    def test_is_scoped_to_the_launch_branch(self, paths):
        argv = self._command(paths).argv
        assert argv[argv.index("--branch") + 1] == "main"

    def test_points_at_the_shared_status_and_sessions_files(self, paths):
        argv = self._command(paths).argv
        assert argv[argv.index("--status-dir") + 1] == str(paths.status_dir)
        assert argv[argv.index("--sessions-file") + 1] == str(paths.sessions_file)

    def test_has_no_role_or_worktree_flag(self, paths):
        # Unlike inbox, a dashboard aggregates every role -- it doesn't watch one.
        argv = self._command(paths).argv
        assert "--role" not in argv
        assert "--worktree" not in argv

    def test_forces_utf8_so_the_glyphs_cannot_crash_it(self, paths):
        assert self._command(paths).env["PYTHONIOENCODING"] == "utf-8"

    def test_runs_no_agent_cli(self, paths):
        argv = self._command(paths).argv
        assert "claude" not in argv
        assert "--permission-mode" not in argv

    def test_writes_a_log_file(self, paths):
        argv = self._command(paths).argv
        assert argv[argv.index("--log-file") + 1].endswith("scheduler-dashboard.log")


class TestInboxIsNotAnAgent:
    """Every per-role generation step must skip it, or it gets an agent's paperwork."""

    def _role(self):
        from launcher.config import RoleConfig

        return RoleConfig(
            role="inbox", worktree="@current", mode="manual",
            scheduler="inbox", watches="human-in-the-loop",
        )

    def test_no_worker_definition_is_written(self, paths):
        from launcher.generate import write_worker_file

        assert write_worker_file(self._role(), paths) is None

    def test_no_instruction_file_is_written(self, tmp_path, paths):
        from launcher.generate import write_instructions

        assert write_instructions(self._role(), paths, "main", tmp_path) is None

    def test_it_never_deletes_the_file_at_its_own_computed_path(self, tmp_path, paths):
        # An inbox always uses "@current" -- the same directory as the real role it
        # watches, by design (a dedicated worktree for a notification-only pane makes no
        # sense). That means instruction_file_for() for an inbox role's config resolves to
        # the SAME path as the watched role's own CLAUDE.md, not a file the inbox ever
        # owned. Regression: this used to unconditionally delete "a stale file for this
        # role" here, which meant an inbox processed after the role it watches (as
        # the default profile's terminals order does) silently erased that role's real,
        # just-written CLAUDE.md -- see TestInstructionFiles.
        # test_an_inbox_pane_does_not_delete_the_role_it_watches_claude_md in
        # test_launcher_generate.py for the end-to-end version of this.
        from launcher.generate import instruction_file_for, write_instructions

        role = self._role()
        existing = instruction_file_for(role, tmp_path)
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("belongs to human-in-the-loop, not the inbox", encoding="utf-8")

        write_instructions(role, paths, "main", tmp_path)
        assert existing.exists()

    def test_it_is_not_treated_as_a_scheduler_role(self, paths):
        # uses_scheduler drives MCP config and worktree branching; an inbox is neither.
        assert self._role().uses_scheduler is False
        assert self._role().is_inbox is True


class TestDashboardIsNotAnAgent:
    """Same class of role as inbox -- see TestInboxIsNotAnAgent and RoleConfig.is_passive."""

    def _role(self):
        from launcher.config import RoleConfig

        return RoleConfig(
            role="dashboard", worktree="@current", mode="manual", scheduler="dashboard"
        )

    def test_no_worker_definition_is_written(self, paths):
        from launcher.generate import write_worker_file

        assert write_worker_file(self._role(), paths) is None

    def test_no_instruction_file_is_written(self, tmp_path, paths):
        from launcher.generate import write_instructions

        assert write_instructions(self._role(), paths, "main", tmp_path) is None

    def test_it_never_deletes_the_file_at_its_own_computed_path(self, tmp_path, paths):
        # Same collision class as the inbox regression: a dashboard also always uses
        # "@current", so instruction_file_for() for its config resolves to whatever real
        # role shares that worktree, not a file the dashboard ever owned.
        from launcher.generate import instruction_file_for, write_instructions

        role = self._role()
        existing = instruction_file_for(role, tmp_path)
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("belongs to human-in-the-loop, not the dashboard", encoding="utf-8")

        write_instructions(role, paths, "main", tmp_path)
        assert existing.exists()

    def test_it_is_not_treated_as_a_scheduler_role(self, paths):
        assert self._role().uses_scheduler is False
        assert self._role().is_dashboard is True
        assert self._role().is_passive is True


class TestPowerShellRendering:
    def test_renders_a_runnable_command(self, paths):
        rendered = render_powershell(build(paths, model="sonnet"))
        assert rendered.startswith("& 'claude'")
        assert "'--model' 'sonnet'" in rendered

    def test_sets_environment_variables_first(self, paths):
        rendered = render_powershell(build(paths, agent="codex"))
        assert rendered.startswith("$env:CODEX_HOME = '")
        assert rendered.index("$env:CODEX_HOME") < rendered.index("& 'codex'")

    def test_quotes_paths_containing_spaces(self, tmp_path):
        paths = KilnPaths.create(tmp_path / "My Projects" / "proj", tmp_path / "fw")
        rendered = render_powershell(build(paths))
        assert "'" in rendered
        assert "My Projects" in rendered

    def test_escapes_embedded_single_quotes(self, paths):
        rendered = render_powershell(AgentCommand(argv=["echo", "it's here"]))
        assert "'it''s here'" in rendered

    def test_includes_the_banner(self, paths):
        assert "Write-Host 'Coder'" in render_powershell(build(paths, agent="copilot"))

    def test_no_clearing_by_default(self, paths):
        # Windows Terminal passes the command as -Command; there is no echo to wipe, and
        # clearing would erase the shell's own startup output for nothing.
        assert "Clear-Host" not in render_powershell(build(paths))

    def test_clears_the_echoed_command_when_asked(self, paths):
        # WezTerm types the command into a live prompt, so the pane would otherwise open on
        # a wall of quoted flags instead of the agent's banner.
        rendered = render_powershell(build(paths), clear=True)
        assert rendered.startswith("Clear-Host; ")


class TestPosixRendering:
    def test_renders_a_runnable_command(self, paths):
        assert render_posix(build(paths, model="sonnet")).startswith("claude --model sonnet")

    def test_exports_environment_variables_first(self, paths):
        rendered = render_posix(build(paths, agent="codex"))
        assert rendered.startswith("export CODEX_HOME=")
        assert rendered.index("export") < rendered.index("codex --dangerously")

    def test_quotes_arguments_with_spaces(self, paths):
        assert "'Start your role session.'" in render_posix(build(paths))

    def test_escapes_shell_metacharacters(self):
        rendered = render_posix(AgentCommand(argv=["echo", "a; rm -rf /"]))
        # The dangerous text must be one quoted argument, not a second command.
        assert rendered == "echo 'a; rm -rf /'"

    def test_clears_the_echoed_command_when_asked(self):
        # tmux send-keys types into a live prompt, same as WezTerm.
        assert render_posix(AgentCommand(argv=["echo", "hi"]), clear=True) == "clear; echo hi"

    def test_scheduler_command_renders_for_tmux(self, paths):
        rendered = render_posix(
            build(paths, scheduler="python", mode="auto", worktree="coder")
        )
        assert "export PYTHONPATH=" in rendered
        assert "python -m scheduler.role_scheduler" in rendered
