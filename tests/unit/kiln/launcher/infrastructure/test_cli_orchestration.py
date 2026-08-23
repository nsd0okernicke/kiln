"""Launcher CLI orchestration at its process and filesystem boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln.launcher.domain.paths import KilnPaths
from kiln.launcher.domain.profile import Profile, RoleConfig
from kiln.launcher.infrastructure import cli


def profile(*roles: RoleConfig) -> Profile:
    return Profile(name="test", description="", roles=list(roles), layout={})


class TestResolveQueueContext:
    def test_explicit_database_context_is_left_untouched(self):
        argv = ["--db-path", "elsewhere.sqlite", "--branch", "feature"]
        assert cli.resolve_queue_context(argv) is argv

    def test_project_context_adds_database_and_current_branch(self, tmp_path, monkeypatch):
        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        paths.state_dir.mkdir(parents=True)
        paths.db_path.touch()
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: paths.framework_root)
        monkeypatch.setattr(cli.workspace, "current_branch", lambda actual: "feature")

        resolved = cli.resolve_queue_context(["--working-dir", str(tmp_path), "--to", "coder"])

        assert resolved == ["--to", "coder", "--db-path", str(paths.db_path), "--branch", "feature"]

    def test_powershell_working_directory_flag_is_consumed(self, tmp_path, monkeypatch):
        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        paths.state_dir.mkdir(parents=True)
        paths.db_path.touch()
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: paths.framework_root)

        resolved = cli.resolve_queue_context(["-WorkingDir", str(tmp_path), "--branch", "given"])

        assert resolved.count("--branch") == 1
        assert "given" in resolved

    def test_missing_queue_explains_that_the_swarm_must_start_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: tmp_path / "framework")

        with pytest.raises(cli.LaunchError, match="Launch the swarm"):
            cli.resolve_queue_context(["--working-dir", str(tmp_path)])

    def test_existing_branch_is_not_replaced(self, tmp_path, monkeypatch):
        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        paths.state_dir.mkdir(parents=True)
        paths.db_path.touch()
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: paths.framework_root)
        monkeypatch.setattr(
            cli.workspace, "current_branch", lambda paths: pytest.fail("branch was resolved")
        )

        result = cli.resolve_queue_context(
            ["--working-dir", str(tmp_path), "--branch", "already-known"]
        )

        assert result[-2:] == ["--db-path", str(paths.db_path)]


class TestStartProxy:
    @pytest.fixture
    def paths(self, tmp_path):
        return KilnPaths.create(tmp_path, tmp_path / "framework")

    def test_launches_a_detached_proxy_with_routes(self, paths, monkeypatch):
        launched = {}
        reclaimed = []
        monkeypatch.setattr(cli.stop, "stop_project_proxies", reclaimed.append)
        monkeypatch.setattr(cli, "find_free_port", lambda port: port + 1)
        monkeypatch.setattr(cli, "wait_until_listening", lambda port: port == 8788)
        monkeypatch.setattr(cli, "python_command", lambda: "python-test")
        monkeypatch.setattr(
            cli.subprocess,
            "Popen",
            lambda command, **kwargs: launched.update(command=command, kwargs=kwargs),
        )

        url = cli.start_proxy(paths, 8787, "full", profile(RoleConfig(role="coder", agent="codex")))

        assert url == "http://127.0.0.1:8788"
        assert reclaimed == [paths.traffic_db]
        assert launched["command"][:3] == [
            "python-test",
            "-m",
            "kiln.proxy.infrastructure.http.server",
        ]
        assert "--route=coder=chatgpt.com/backend-api/codex" in launched["command"]
        assert launched["kwargs"]["cwd"] == str(paths.project_root)

    def test_explicit_port_is_not_silently_changed(self, paths, monkeypatch):
        monkeypatch.setattr(cli.stop, "stop_project_proxies", lambda path: [])
        monkeypatch.setattr(
            cli, "find_free_port", lambda port: pytest.fail("must not probe an explicit port")
        )
        monkeypatch.setattr(cli, "wait_until_listening", lambda port: True)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: None)

        assert cli.start_proxy(paths, 9000, "metadata", profile(), port_is_explicit=True).endswith(
            ":9000"
        )

    def test_process_start_failure_becomes_a_launch_error(self, paths, monkeypatch):
        monkeypatch.setattr(cli.stop, "stop_project_proxies", lambda path: [])
        monkeypatch.setattr(cli, "find_free_port", lambda port: port)
        monkeypatch.setattr(
            cli.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no"))
        )

        with pytest.raises(cli.LaunchError, match="could not start"):
            cli.start_proxy(paths, 8787, "metadata", profile())

    def test_process_that_never_listens_is_reported(self, paths, monkeypatch):
        monkeypatch.setattr(cli.stop, "stop_project_proxies", lambda path: [])
        monkeypatch.setattr(cli, "find_free_port", lambda port: port)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: None)
        monkeypatch.setattr(cli, "wait_until_listening", lambda port: False)

        with pytest.raises(cli.LaunchError, match="did not start listening"):
            cli.start_proxy(paths, 8787, "metadata", profile())


class TestRunLaunch:
    def args(self, root: Path, **overrides):
        values = dict(
            working_dir=str(root),
            profile="test",
            agent_override="",
            model_override="",
            terminal="auto",
            proxy=False,
            dry_run=False,
            proxy_port=8787,
            capture="metadata",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def wire(self, root, monkeypatch, selected_profile):
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: root / "framework")
        monkeypatch.setattr(cli, "check_dependencies", lambda: None)
        monkeypatch.setattr(cli, "load_profile", lambda *args: selected_profile)
        monkeypatch.setattr(cli, "check_launchable", lambda value: None)
        monkeypatch.setattr(cli, "warn_if_channel_unavailable", lambda value: None)
        monkeypatch.setattr(cli, "detect_backend", lambda value: "tmux")
        monkeypatch.setattr(cli, "prepare", lambda *args: "feature")
        monkeypatch.setattr(cli, "build_panes", lambda *args, **kwargs: [])

    def test_missing_working_directory_fails_before_setup(self, tmp_path):
        with pytest.raises(cli.LaunchError, match="working directory does not exist"):
            cli.run_launch(self.args(tmp_path / "missing"))

    def test_dry_run_describes_terminal_without_starting_proxy(self, tmp_path, monkeypatch, capsys):
        selected = profile(RoleConfig(role="coder", agent="claude"))
        self.wire(tmp_path, monkeypatch, selected)
        monkeypatch.setattr(cli, "start_proxy", lambda *args, **kwargs: pytest.fail("spawned"))
        monkeypatch.setattr(cli, "launch_terminal", lambda *args, **kwargs: ["tmux", "new"])

        assert cli.run_launch(self.args(tmp_path, proxy=True, dry_run=True)) == 0
        assert "would launch" in capsys.readouterr().out

    def test_live_proxy_url_is_passed_to_pane_construction(self, tmp_path, monkeypatch):
        selected = profile(
            RoleConfig(role="coder", agent="claude"),
            RoleConfig(role="reviewer", agent="copilot"),
        )
        self.wire(tmp_path, monkeypatch, selected)
        received = {}
        monkeypatch.setattr(cli, "start_proxy", lambda *args, **kwargs: "http://proxy")
        monkeypatch.setattr(
            cli,
            "build_panes",
            lambda *args, **kwargs: received.update(kwargs) or [],
        )
        monkeypatch.setattr(cli, "launch_terminal", lambda *args, **kwargs: [])

        assert cli.run_launch(self.args(tmp_path, proxy=True)) == 0
        assert received["proxy_url"] == "http://proxy"

    def test_agent_override_is_applied_before_validation(self, tmp_path, monkeypatch):
        selected = profile(RoleConfig(role="coder", agent="claude"))
        self.wire(tmp_path, monkeypatch, selected)
        overridden = profile(RoleConfig(role="coder", agent="codex"))
        seen = []
        monkeypatch.setattr(cli, "apply_agent_override", lambda *args: overridden)
        monkeypatch.setattr(cli, "check_launchable", seen.append)
        monkeypatch.setattr(cli, "launch_terminal", lambda *args, **kwargs: [])

        cli.run_launch(self.args(tmp_path, agent_override="codex", model_override="o3"))

        assert seen == [overridden]

    def test_each_pane_kind_and_dry_run_command_are_described(
        self, tmp_path, monkeypatch, caplog, capsys
    ):
        selected = profile(
            RoleConfig(role="inbox", scheduler="inbox", watches="human"),
            RoleConfig(role="dashboard", scheduler="dashboard"),
            RoleConfig(role="cockpit", scheduler="cockpit"),
            RoleConfig(role="coder", agent="claude", scheduler="python"),
            RoleConfig(role="human", agent="codex", mode="manual"),
        )
        self.wire(tmp_path, monkeypatch, selected)
        pane = SimpleNamespace(role="coder", path=tmp_path, cmd="python worker.py")
        monkeypatch.setattr(cli, "build_panes", lambda *args, **kwargs: [pane])
        monkeypatch.setattr(cli, "launch_terminal", lambda *args, **kwargs: ["tmux", "new"])

        with caplog.at_level("INFO"):
            cli.run_launch(self.args(tmp_path, dry_run=True))

        assert "inbox -> human" in caplog.text
        assert "dashboard" in caplog.text
        assert "cockpit (browser)" in caplog.text
        assert "claude [scheduler]" in caplog.text
        assert "\n[coder]" in capsys.readouterr().out


class TestPrepare:
    def test_prepares_shared_state_then_each_role_and_records_sessions(self, tmp_path, monkeypatch):
        from kiln.scheduler.infrastructure.persistence import db

        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        selected = profile(
            RoleConfig(role="human", mode="manual", worktree="@current"),
            RoleConfig(role="coder", scheduler="python", worktree="coder"),
        )
        calls = []
        monkeypatch.setattr(cli.sys, "path", list(cli.sys.path))
        monkeypatch.setattr(cli, "check_project_scaffolding", lambda p: calls.append("check"))
        for name in (
            "initialize_repo",
            "install_git_hooks",
            "warn_if_kiln_untracked",
            "prepare_state_dirs",
            "copy_framework_tools",
            "prepare_worktrees",
            "prepare_skills",
            "prepare_agent_configs",
            "write_sessions_file",
        ):
            monkeypatch.setattr(
                cli.workspace, name, lambda *args, _name=name, **kwargs: calls.append(_name)
            )
        monkeypatch.setattr(cli.workspace, "current_branch", lambda p: "feature")
        monkeypatch.setattr(
            cli.workspace, "worktree_for", lambda role, p: p.project_root / role.role
        )
        monkeypatch.setattr(db, "ensure_schema", lambda path: calls.append(("schema", path)))
        monkeypatch.setattr(
            cli.generate,
            "channel_is_available",
            lambda role: calls.append(("channel", role.role if role else None)) or True,
        )
        monkeypatch.setattr(
            cli.generate,
            "write_mcp_config",
            lambda *args, **kwargs: calls.append(("mcp", args[2], kwargs["include_channel"])),
        )
        monkeypatch.setattr(
            cli.generate, "write_worker_file", lambda role, p: calls.append(("worker", role.role))
        )
        monkeypatch.setattr(
            cli.generate,
            "write_instructions",
            lambda role, *args: calls.append(("instructions", role.role)),
        )
        monkeypatch.setattr(cli, "_copy_root_settings", lambda p: calls.append("settings"))

        assert cli.prepare(selected, paths) == "feature"
        assert calls.index("copy_framework_tools") < calls.index(("schema", paths.db_path))
        assert ("mcp", "human", True) in calls
        assert [item for item in calls if isinstance(item, tuple) and item[0] == "worker"] == [
            ("worker", "human"),
            ("worker", "coder"),
        ]
        instructions = [
            item for item in calls if isinstance(item, tuple) and item[0] == "instructions"
        ]
        assert instructions == [("instructions", "human"), ("instructions", "coder")]
        assert calls[-1] == "write_sessions_file"

    def test_profile_without_current_directory_passes_no_mcp_owner(self, tmp_path, monkeypatch):
        from kiln.scheduler.infrastructure.persistence import db

        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        selected = profile(RoleConfig(role="coder", scheduler="python", worktree="coder"))
        owners = []
        monkeypatch.setattr(cli, "check_project_scaffolding", lambda p: None)
        monkeypatch.setattr(cli.sys, "path", list(cli.sys.path))
        for name in (
            "initialize_repo",
            "install_git_hooks",
            "warn_if_kiln_untracked",
            "prepare_state_dirs",
            "copy_framework_tools",
            "prepare_worktrees",
            "prepare_skills",
            "prepare_agent_configs",
            "write_sessions_file",
        ):
            monkeypatch.setattr(cli.workspace, name, lambda *args, **kwargs: None)
        monkeypatch.setattr(cli.workspace, "current_branch", lambda p: "main")
        monkeypatch.setattr(cli.workspace, "worktree_for", lambda role, p: tmp_path)
        monkeypatch.setattr(db, "ensure_schema", lambda path: None)
        monkeypatch.setattr(cli.generate, "channel_is_available", lambda role: role is not None)
        monkeypatch.setattr(
            cli.generate,
            "write_mcp_config",
            lambda root, p, owner, *args, **kwargs: owners.append(owner),
        )
        monkeypatch.setattr(cli.generate, "write_worker_file", lambda *args: None)
        monkeypatch.setattr(cli.generate, "write_instructions", lambda *args: None)
        monkeypatch.setattr(cli, "_copy_root_settings", lambda p: None)

        cli.prepare(selected, paths)

        assert owners == [None]


class TestMainDispatch:
    def test_queue_subcommand_is_delegated_before_main_parser(self, monkeypatch):
        monkeypatch.setattr(cli, "run_subcommand", lambda name, argv: 7)
        assert cli.main(["send", "--to", "coder"]) == 7

    def test_queue_context_error_is_reported_as_exit_one(self, monkeypatch):
        monkeypatch.setattr(
            cli, "run_subcommand", lambda *args: (_ for _ in ()).throw(cli.LaunchError("bad"))
        )
        assert cli.main(["retry", "abc"]) == 1

    @pytest.mark.parametrize(
        ("argv", "handler"),
        [
            (["--list-profiles"], "run_list_profiles"),
            (["--init"], "run_init"),
            (["--stop"], "run_stop"),
            ([], "run_launch"),
        ],
    )
    def test_top_level_mode_dispatch(self, argv, handler, monkeypatch):
        called = []
        monkeypatch.setattr(cli, handler, lambda args: called.append(args) or 4)
        assert cli.main(argv) == 4
        assert len(called) == 1

    def test_known_operational_error_returns_one(self, monkeypatch):
        monkeypatch.setattr(
            cli, "run_launch", lambda args: (_ for _ in ()).throw(cli.TerminalError("missing"))
        )
        assert cli.main([]) == 1

    def test_keyboard_interrupt_returns_shell_interrupt_code(self, monkeypatch):
        monkeypatch.setattr(
            cli, "run_launch", lambda args: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        assert cli.main([]) == 130


class TestRunStop:
    def test_passes_every_recorded_role_and_dry_run_to_stop(self, tmp_path, monkeypatch):
        paths = KilnPaths.create(tmp_path, tmp_path / "framework")
        paths.sessions_file.parent.mkdir(parents=True)
        paths.sessions_file.write_text(
            "1\thuman\tclaude\tHuman\nmalformed\n2\tcoder\tclaude\tCoder\n",
            encoding="utf-8",
        )
        seen = []
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: paths.framework_root)
        monkeypatch.setattr(
            cli.stop,
            "stop_all",
            lambda roles, dry_run: seen.append((roles, dry_run)) or [10, 11],
        )

        result = cli.run_stop(SimpleNamespace(working_dir=str(tmp_path), dry_run=True))

        assert result == 0
        assert seen == [(["human", "coder"], True)]

    def test_missing_sessions_file_still_stops_machine_wide_processes(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "resolve_framework_root", lambda: tmp_path / "framework")
        monkeypatch.setattr(
            cli.stop,
            "stop_all",
            lambda roles, dry_run: seen.append((roles, dry_run)) or [],
        )

        cli.run_stop(SimpleNamespace(working_dir=str(tmp_path), dry_run=False))

        assert seen == [([], False)]
