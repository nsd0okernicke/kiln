"""
Pane command construction.

This is the one seam between the tested Python core and a real terminal: whatever these
produce is typed verbatim into a shell. A quoting mistake here is invisible until a pane
silently runs the wrong thing, so the renderers are tested against paths with spaces and
quotes rather than only tidy ones.
"""

from __future__ import annotations

import shutil

import pytest
from launcher.commands import (
    START_PROMPT,
    AgentCommand,
    build_agent_command,
    proxy_env,
    render_posix,
    render_powershell,
)
from launcher.config import Profile, RoleConfig
from launcher.paths import KilnPaths, python_command
from scheduler.routing import parse_profile_routing


@pytest.fixture
def paths(tmp_path):
    return KilnPaths.create(tmp_path / "proj", tmp_path / "fw")


def build(paths, proxy_url=None, **role_kwargs):
    role_kwargs.setdefault("role", "coder")
    return build_agent_command(
        RoleConfig(**role_kwargs), paths, branch="main", proxy_url=proxy_url
    )


PROXY = "http://127.0.0.1:8787"


class TestProxyEnv:
    def test_points_a_claude_role_at_its_own_path_prefix(self):
        # The prefix is what makes a capture attributable: a proxy sees HTTP, not roles.
        env = proxy_env(RoleConfig(role="coder", agent="claude"), PROXY)
        assert env == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/kiln/coder"}

    def test_hyphenated_roles_survive(self):
        env = proxy_env(RoleConfig(role="human-in-the-loop", agent="claude"), PROXY)
        assert env["ANTHROPIC_BASE_URL"].endswith("/kiln/human-in-the-loop")

    def test_a_trailing_slash_does_not_double_up(self):
        env = proxy_env(RoleConfig(role="coder", agent="claude"), "http://127.0.0.1:8787/")
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787/kiln/coder"

    def test_no_proxy_means_no_env(self):
        assert proxy_env(RoleConfig(role="coder", agent="claude"), None) == {}

    @pytest.mark.parametrize("agent", ["copilot", "grok"])
    def test_unverified_backends_are_left_alone(self, agent):
        # claude and codex have both been verified live. Guessing at the rest would either
        # do nothing or break their auth, silently.
        assert proxy_env(RoleConfig(role="coder", agent=agent), PROXY) == {}

    def test_codex_gets_kilns_own_variable(self):
        # Codex has no base-URL variable of its own, so Kiln carries the URL in one it owns
        # and the adapter turns it into `-c` flags at call time.
        env = proxy_env(RoleConfig(role="coder", agent="codex"), PROXY)
        assert env == {"KILN_PROXY_BASE_URL": "http://127.0.0.1:8787/kiln/coder"}


class TestProxyWiring:
    def test_a_scheduler_role_gets_the_base_url(self, paths):
        # The one-shot worker is a subprocess of this pane and inherits its environment,
        # so setting it here covers the worker too.
        command = build(paths, agent="claude", scheduler="python", mode="auto", proxy_url=PROXY)
        assert command.env["ANTHROPIC_BASE_URL"] == f"{PROXY}/kiln/coder"

    def test_a_wrapper_role_gets_the_base_url(self, paths):
        command = build(paths, agent="claude", mode="manual", proxy_url=PROXY)
        assert command.env["ANTHROPIC_BASE_URL"] == f"{PROXY}/kiln/coder"

    def test_a_codex_wrapper_pane_carries_the_overrides_on_its_argv(self, paths):
        # Codex reads no base-URL env var, so the pane's own command needs the flags. The
        # env var is set too, for the one-shot worker the scheduler spawns from this pane.
        command = build(paths, agent="codex", mode="manual", proxy_url=PROXY)
        argv = " ".join(command.argv)
        assert f'base_url="{PROXY}/kiln/coder"' in argv
        assert 'wire_api="responses"' in argv
        assert command.env["KILN_PROXY_BASE_URL"] == f"{PROXY}/kiln/coder"

    def test_a_codex_pane_is_untouched_without_the_proxy(self, paths):
        command = build(paths, agent="codex", mode="manual")
        assert not any(argument == "-c" for argument in command.argv)
        assert "KILN_PROXY_BASE_URL" not in command.env

    def test_the_scheduler_keeps_its_own_env(self, paths):
        # with_env must add to PYTHONPATH/PYTHONIOENCODING, not replace them.
        command = build(paths, agent="claude", scheduler="python", mode="auto", proxy_url=PROXY)
        assert "PYTHONPATH" in command.env
        assert command.env["PYTHONIOENCODING"] == "utf-8"

    def test_no_proxy_leaves_the_command_unchanged(self, paths):
        assert "ANTHROPIC_BASE_URL" not in build(paths, agent="claude").env

    def test_a_codex_role_is_not_routed(self, paths):
        command = build(paths, agent="codex", mode="manual", proxy_url=PROXY)
        assert "ANTHROPIC_BASE_URL" not in command.env
        assert command.env["CODEX_HOME"]  # its own env is untouched

    def test_the_dashboard_is_always_told_where_the_capture_store_is(self, paths):
        # Unconditional: the dashboard hides the panel when the store is absent, so one
        # code path covers proxied and unproxied launches alike.
        command = build(paths, role="dashboard", scheduler="dashboard", mode="manual")
        assert "--traffic-db" in command.argv
        assert str(paths.traffic_db) in command.argv


class TestCockpitPane:
    """
    The cockpit pane (issue #22). It is a passive pane like the dashboard, but two of its
    flags come from the *profile* rather than from paths, because the cockpit deliberately
    does not parse profiles itself.
    """

    def _profile(self, **overrides):
        roles = overrides.pop("roles", None) or (
            RoleConfig(role="human-in-the-loop", worktree="@current", mode="manual"),
            RoleConfig(role="specifier", scheduler="python"),
            RoleConfig(role="coder", scheduler="python"),
            RoleConfig(role="dashboard", scheduler="dashboard", mode="manual"),
            RoleConfig(role="cockpit", scheduler="cockpit", mode="manual"),
        )
        routing = overrides.pop("routing", None) or parse_profile_routing({
            "human-in-the-loop": "specifier",
            "specifier": "coder",
            "coder": "human-in-the-loop",
        })
        return Profile(name="test", roles=roles, routing=routing, **overrides)

    def _cockpit(self, paths, profile=None, **role_kwargs):
        role_kwargs.setdefault("role", "cockpit")
        role_kwargs.setdefault("scheduler", "cockpit")
        role_kwargs.setdefault("mode", "manual")
        return build_agent_command(
            RoleConfig(**role_kwargs), paths, branch="main",
            profile=profile if profile is not None else self._profile(),
        )

    def test_it_runs_the_cockpit_server_and_not_an_agent(self, paths):
        argv = self._cockpit(paths).argv

        assert argv[1:3] == ["-m", "cockpit.server"]

    def test_it_reads_the_same_state_the_dashboard_does(self, paths):
        # A second front end over one set of numbers, not a second source of them.
        argv = self._cockpit(paths).argv

        assert argv[argv.index("--db-path") + 1] == str(paths.db_path)
        assert argv[argv.index("--status-dir") + 1] == str(paths.status_dir)
        assert argv[argv.index("--sessions-file") + 1] == str(paths.sessions_file)

    def test_lanes_come_from_routing_in_profile_order(self, paths):
        # Not every role: `inbox`, `dashboard` and the cockpit itself hand nothing off, so a
        # lane for them would be permanently empty by construction.
        argv = self._cockpit(paths).argv

        assert argv[argv.index("--lanes") + 1] == "human-in-the-loop,specifier,coder"

    def test_new_task_targets_whatever_routing_says_the_human_hands_off_to(self, paths):
        # Asking the table is the only answer that cannot disagree with the swarm's own
        # first hop.
        argv = self._cockpit(paths).argv

        assert argv[argv.index("--intake-role") + 1] == "specifier"

    def test_the_human_role_is_the_profiles_manual_role(self, paths):
        profile = self._profile(roles=(
            RoleConfig(role="operator", worktree="@current", mode="manual"),
            RoleConfig(role="coder", scheduler="python"),
            RoleConfig(role="cockpit", scheduler="cockpit", mode="manual"),
        ), routing=parse_profile_routing({"operator": "coder", "coder": "operator"}))

        argv = self._cockpit(paths, profile=profile).argv

        assert argv[argv.index("--human-role") + 1] == "operator"

    def test_a_profile_with_no_manual_role_still_launches(self, paths):
        # A cockpit that refused to start over a naming convention would be worse than one
        # that falls back to the conventional name.
        profile = self._profile(roles=(
            RoleConfig(role="coder", scheduler="python"),
            RoleConfig(role="cockpit", scheduler="cockpit", mode="manual"),
        ), routing=parse_profile_routing({"coder": "coder"}))

        argv = self._cockpit(paths, profile=profile).argv

        assert argv[argv.index("--human-role") + 1] == "human-in-the-loop"

    def test_it_records_where_it_bound(self, paths):
        argv = self._cockpit(paths).argv

        assert argv[argv.index("--url-file") + 1] == str(paths.cockpit_url_file)
        assert argv[argv.index("--pid-file") + 1] == str(paths.cockpit_pid_file)

    def test_a_configured_port_is_passed_through(self, paths):
        argv = self._cockpit(paths, port=9100).argv

        assert argv[argv.index("--port") + 1] == "9100"

    def test_an_unset_port_leaves_the_modules_own_default_alone(self, paths):
        # Two places to change one number is how they drift.
        assert "--port" not in self._cockpit(paths).argv

    def test_opting_out_of_the_browser_reaches_the_command(self, paths):
        assert "--no-browser" in self._cockpit(paths, open_browser=False).argv

    def test_the_browser_opens_by_default(self, paths):
        assert "--no-browser" not in self._cockpit(paths).argv

    def test_it_sets_pythonpath_so_the_package_resolves(self, paths):
        env = self._cockpit(paths).env

        assert env["PYTHONPATH"].replace("\\", "/").endswith("kiln/framework")


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


class TestGrok:
    def test_launches_grok_with_the_start_prompt(self, paths):
        argv = build(paths, agent="grok").argv
        assert argv[0] == "grok"
        assert argv[-1] == START_PROMPT

    def test_auto_mode_bypasses_permissions(self, paths):
        # `--permission-mode` rather than `--always-approve`, which the one-shot adapter
        # uses: only the former can express the manual/auto split, and a manual role is
        # human-supervised. Both flags exist on this CLI (verified against grok 1.0.5).
        argv = build(paths, agent="grok", mode="auto").argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    def test_manual_mode_keeps_prompts(self, paths):
        argv = build(paths, agent="grok", mode="manual").argv
        assert argv[argv.index("--permission-mode") + 1] == "default"

    def test_keeps_subagents_enabled(self, paths):
        # The mirror image of the scheduler adapter, which passes --no-subagents to isolate a
        # one-shot worker. A wrapper *is* the delegator: without spawn_subagent it cannot
        # reach `<role>-worker` at all and would have to do the work itself.
        assert "--no-subagents" not in build(paths, agent="grok").argv

    def test_model_is_optional(self, paths):
        assert "-m" not in build(paths, agent="grok").argv
        argv = build(paths, agent="grok", model="grok-4.5").argv
        assert argv[argv.index("-m") + 1] == "grok-4.5"

    def test_gets_a_banner_because_its_cli_shows_no_role(self, paths):
        # Same reason as Copilot: there is no `-n`/`--name` equivalent on this CLI.
        assert build(paths, agent="grok").banner == "Coder"

    def test_debug_log_is_named_for_grok_not_claude(self, paths):
        argv = build(paths, agent="grok").argv
        assert "claude-debug" not in argv[argv.index("--debug-file") + 1]
        assert "grok-debug-coder.log" in argv[argv.index("--debug-file") + 1]


class TestUnsupportedAgent:
    def test_reports_in_the_pane_instead_of_failing_the_launch(self, paths):
        # Unreachable for anything in VALID_AGENTS -- all four now have a launch path. This
        # pins the behaviour for the next backend added there before it has one: one pane
        # says so, rather than the whole swarm failing to start.
        argv = build(paths, agent="some-future-agent").argv
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
        assert argv[:3] == [python_command(), "-m", "scheduler.role_scheduler"]

    def test_names_an_interpreter_that_actually_exists(self, paths):
        # The literal "python" was hardcoded here, and stock Debian/Ubuntu ships only
        # `python3` -- every scheduler pane, the inbox and the dashboard died instantly with
        # "Command 'python' not found" (confirmed on Ubuntu 24.04). Whatever name is chosen
        # has to resolve on the host actually doing the launching.
        assert shutil.which(self._scheduler(paths).argv[0]) is not None

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

    def test_termination_guards_reach_the_scheduler(self, paths):
        # A guard configured in a profile and never passed through is a guard that does not
        # exist -- which is exactly what happened to claude_adapter's --max-budget-usd, fully
        # implemented and unit-tested with no caller.
        argv = self._scheduler(paths, max_cycles=6, max_budget_usd=12.5).argv
        assert argv[argv.index("--max-cycles") + 1] == "6"
        assert argv[argv.index("--max-budget-usd") + 1] == "12.5"

    def test_a_verify_command_reaches_the_scheduler(self, paths):
        argv = self._scheduler(paths, verify="pytest -q", verify_timeout=120).argv
        assert argv[argv.index("--verify") + 1] == "pytest -q"
        assert argv[argv.index("--verify-timeout") + 1] == "120"

    def test_a_verify_timeout_without_a_command_is_not_passed(self, paths):
        # A timeout for a gate that does not exist would be meaningless on the command line.
        argv = self._scheduler(paths, verify_timeout=120).argv
        assert "--verify-timeout" not in argv

    def test_profile_knobs_reach_the_scheduler(self, paths):
        argv = self._scheduler(
            paths, poll_interval=5, worker_timeout=60, max_attempts=4, escalation_limit=2
        ).argv
        assert argv[argv.index("--poll-interval") + 1] == "5"
        assert argv[argv.index("--worker-timeout") + 1] == "60"
        assert argv[argv.index("--max-attempts") + 1] == "4"
        assert argv[argv.index("--escalation-limit") + 1] == "2"

    def test_a_fractional_poll_interval_is_not_rendered_in_scientific_notation(self, paths):
        argv = self._scheduler(paths, poll_interval=0.5).argv
        assert argv[argv.index("--poll-interval") + 1] == "0.5"

    def test_unset_knobs_are_not_passed_so_the_module_default_applies(self, paths):
        argv = self._scheduler(paths).argv
        for flag in ("--poll-interval", "--worker-timeout", "--max-attempts",
                     "--escalation-limit"):
            assert flag not in argv

    def test_unset_guards_are_not_passed_at_all(self, paths):
        # So the scheduler's own "no ceiling" default applies, rather than a number chosen
        # here leaking in as a de facto policy.
        argv = self._scheduler(paths).argv
        assert "--max-cycles" not in argv
        assert "--max-budget-usd" not in argv

    def test_prefers_the_worker_model(self, paths):
        argv = self._scheduler(paths, model="opus", worker_model="sonnet").argv
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_never_launches_the_agent_cli(self, paths):
        # `claude` still appears as the --agent VALUE (which adapter the scheduler uses),
        # but nothing invokes the CLI: the scheduler spawns workers itself, one shot each.
        command = self._scheduler(paths)
        assert command.argv[0] == python_command()
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
        assert argv[:3] == [python_command(), "-m", "scheduler.inbox"]

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

    def test_its_knobs_are_reachable_from_the_profile(self, paths):
        # The inbox accepted --poll-interval and --no-bell all along; nothing could set them.
        argv = self._command(paths, poll_interval=5, bell=False).argv
        assert argv[argv.index("--poll-interval") + 1] == "5"
        assert "--no-bell" in argv

    def test_the_bell_rings_by_default(self, paths):
        assert "--no-bell" not in self._command(paths).argv

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

    def _command(self, paths, **overrides):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig

        role = RoleConfig(
            role="dashboard", worktree="@current", mode="manual", scheduler="dashboard",
            **overrides,
        )
        return build_agent_command(role, paths, "main")

    def test_runs_the_dashboard_module(self, paths):
        argv = self._command(paths).argv
        assert argv[:3] == [python_command(), "-m", "scheduler.dashboard"]

    def test_its_knobs_are_reachable_from_the_profile(self, paths):
        argv = self._command(paths, poll_interval=5, activity_limit=20).argv
        assert argv[argv.index("--poll-interval") + 1] == "5"
        assert argv[argv.index("--activity-limit") + 1] == "20"

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
        assert f"{python_command()} -m scheduler.role_scheduler" in rendered


class TestProfileRoutingReachesTheScheduler:
    """
    The scheduler is a separate process and cannot read the launcher's parsed profile, so a
    profile's own routing has to travel to it as arguments.
    """

    def _profile(self, routing=None):
        from launcher.config import Profile, RoleConfig
        from scheduler.routing import parse_profile_routing

        roles = (
            RoleConfig(role="coder", scheduler="python", mode="auto"),
            RoleConfig(role="architect", scheduler="python", mode="auto"),
            RoleConfig(role="human-in-the-loop"),
        )
        return Profile(
            name="p", description="", roles=roles, layout={},
            routing=parse_profile_routing(routing),
        )

    def test_declared_routing_becomes_route_arguments(self, paths):
        profile = self._profile({"architect": "human-in-the-loop"})
        command = build_agent_command(
            profile.role("architect"), paths, "main", profile=profile
        )
        assert "--route" in command.argv
        assert "architect=human-in-the-loop" in command.argv

    def test_no_declared_routing_passes_no_route_arguments(self, paths):
        # A profile with no routing of its own keeps reading --workflow, which is what
        # every role-complete profile does.
        command = build_agent_command(
            self._profile().role("coder"), paths, "main", profile=self._profile()
        )
        assert "--route" not in command.argv

    def test_the_workflow_path_is_still_passed_either_way(self, paths):
        # The scheduler needs it for the no-routing case, and dropping it would make the
        # two paths diverge in a way nothing else would catch.
        profile = self._profile({"architect": "human-in-the-loop"})
        command = build_agent_command(
            profile.role("architect"), paths, "main", profile=profile
        )
        assert "--workflow" in command.argv

    def test_a_scheduler_role_still_launches_without_a_profile(self, paths):
        # build_agent_command's profile argument is optional; callers that predate it must
        # keep working rather than raising.
        command = build_agent_command(
            self._profile().role("coder"), paths, "main"
        )
        assert "scheduler.role_scheduler" in " ".join(command.argv)
