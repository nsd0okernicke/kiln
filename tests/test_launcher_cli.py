"""
Scaffolding and the CLI entry point.

Scaffolding runs against a real framework tree and a real git repo — it is almost entirely
file copying, so a mocked filesystem would test nothing worth knowing.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
from launcher import cli, scaffold
from launcher.paths import KilnPaths

pytestmark = pytest.mark.integration


@pytest.fixture
def framework(tmp_path):
    """A minimal but structurally real framework checkout."""
    root = tmp_path / "framework"
    bundled = root / "kiln"

    constitution = bundled / "project" / "constitution"
    constitution.mkdir(parents=True)
    for name in scaffold.CONSTITUTION_FILES:
        (constitution / name).write_text(f"# {name}\n", encoding="utf-8")
    (bundled / "project" / "constitution.md").write_text("# Constitution\n", encoding="utf-8")

    roles = bundled / "project" / "roles"
    roles.mkdir(parents=True)
    for role in ("coder", "specifier"):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")

    skill = bundled / "project" / "skills" / "kiln-handoff"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: kiln-handoff\n---\n", encoding="utf-8")

    (bundled / ".claude").mkdir(parents=True)
    (bundled / ".claude" / "settings.json").write_text('{"x": 1}', encoding="utf-8")

    example = root / "examples" / "demo"
    (example / "kiln" / "project" / "constitution").mkdir(parents=True)
    (example / "README.md").write_text("# Demo Brief\n", encoding="utf-8")
    (example / "kiln" / "project" / "constitution" / "project.md").write_text(
        "# Demo project rules\n", encoding="utf-8"
    )
    return root


class TestScaffold:
    def test_creates_the_expected_tree(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert paths.constitution_dir.is_dir()
        assert paths.roles_dir.is_dir()
        assert paths.skills_dir.is_dir()
        assert paths.state_dir.is_dir()

    def test_copies_the_constitution_and_roles(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert (paths.constitution_dir / "workflow.md").is_file()
        assert (paths.kiln_project_dir / "constitution.md").is_file()
        assert (paths.roles_dir / "coder.md").is_file()

    def test_copies_skills_as_real_directories(self, tmp_path, framework):
        # Copied, not linked: they become the user's own editable content.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        assert (paths.skills_dir / "kiln-handoff" / "SKILL.md").is_file()

    def test_stale_skill_copies_are_replaced(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        paths = KilnPaths.create(target, framework)
        stale = paths.skills_dir / "kiln-handoff" / "STALE.md"
        stale.write_text("old", encoding="utf-8")

        scaffold.scaffold(target, framework)

        assert not stale.exists(), "a prior partial copy must not linger"

    def test_initial_mcp_has_db_only(self, tmp_path, framework):
        # kiln-channel is role-scoped and no role exists yet.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
        assert set(config["mcpServers"]) == {"kiln-db"}

    def test_creates_the_message_database(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert (target / ".kiln" / "messages.db").is_file()

    def test_database_has_the_real_schema(self, tmp_path, framework):
        import sqlite3

        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        with sqlite3.connect(target / ".kiln" / "messages.db") as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert {"sender", "target", "status", "content", "branch"} <= columns

    def test_initialises_git_without_committing(self, tmp_path, framework):
        # The scaffold cannot know what else belongs in the project's first commit.
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert (target / ".git").exists()
        from launcher import workspace

        assert workspace.run_git(["rev-parse", "HEAD"], target).returncode != 0

    def test_no_git_flag_is_honoured(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework, no_git=True)
        assert not (target / ".git").exists()

    def test_gitignore_is_written(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        assert ".kiln" in (target / ".gitignore").read_text(encoding="utf-8")

    def test_example_brief_and_overrides_are_applied(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework, example="demo")
        assert "Demo Brief" in (target / "README.md").read_text(encoding="utf-8")
        project_md = target / "kiln" / "project" / "constitution" / "project.md"
        assert "Demo project rules" in project_md.read_text(encoding="utf-8")

    def test_unknown_example_warns_without_failing(self, tmp_path, framework):
        result = scaffold.scaffold(tmp_path / "proj", framework, example="nope")
        assert any("not found" in warning for warning in result.warnings)

    def test_is_idempotent(self, tmp_path, framework):
        target = tmp_path / "proj"
        scaffold.scaffold(target, framework)
        scaffold.scaffold(target, framework)
        assert (target / "kiln" / "project" / "roles" / "coder.md").is_file()

    def test_missing_framework_content_is_fatal(self, tmp_path):
        with pytest.raises(scaffold.ScaffoldError, match="framework content not found"):
            scaffold.scaffold(tmp_path / "proj", tmp_path / "empty")


class TestChannelPreflight:
    """
    Found live: with mcp 2.0.0 installed, `mcp.server.fastmcp` no longer exists, so
    kiln-channel never started. Nothing reported it — the wrapper roles simply could not
    receive handoffs and began asking their human for instructions instead, which reads as a
    confused agent rather than a missing dependency.
    """

    def _profile(self, *scheduled):
        from launcher.config import Profile, RoleConfig

        roles = [
            RoleConfig(role=name, scheduler="python" if name in scheduled else None)
            for name in ("human-in-the-loop", "coder")
        ]
        return Profile(name="p", description="", roles=roles, layout={})

    def test_warns_when_the_sdk_cannot_be_imported(self, monkeypatch, caplog):
        monkeypatch.setattr(cli.shutil, "which", lambda _c: "python")
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 1, "", "ModuleNotFoundError: No module named 'mcp.server.fastmcp'"
            ),
        )
        with caplog.at_level(logging.WARNING):
            assert cli.warn_if_channel_unavailable(self._profile()) is True
        assert "kiln-channel" in caplog.text

    def test_the_warning_names_the_affected_roles_and_a_remedy(self, monkeypatch, caplog):
        # A warning that does not say what broke or how to fix it is barely better than none.
        monkeypatch.setattr(cli.shutil, "which", lambda _c: "python")
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"),
        )
        with caplog.at_level(logging.WARNING):
            cli.warn_if_channel_unavailable(self._profile())
        assert "coder" in caplog.text
        assert "human-in-the-loop" in caplog.text
        assert "pip install" in caplog.text

    def test_silent_when_the_sdk_imports(self, monkeypatch, caplog):
        monkeypatch.setattr(cli.shutil, "which", lambda _c: "python")
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
        )
        with caplog.at_level(logging.WARNING):
            assert cli.warn_if_channel_unavailable(self._profile()) is False
        assert caplog.text == ""

    def test_a_fully_scheduled_swarm_needs_no_mcp_at_all(self, monkeypatch):
        # Scheduler roles talk to SQLite directly, so probing would be noise.
        def fail(*a, **k):
            raise AssertionError("must not probe when nothing uses the channel")

        monkeypatch.setattr(cli.subprocess, "run", fail)
        profile = self._profile("human-in-the-loop", "coder")
        assert cli.warn_if_channel_unavailable(profile) is False

    def test_warns_when_python_is_not_on_path(self, monkeypatch, caplog):
        # .mcp.json names the bare command, which the agent CLI resolves from PATH — not
        # necessarily the interpreter running the launcher.
        monkeypatch.setattr(cli.shutil, "which", lambda _c: None)
        with caplog.at_level(logging.WARNING):
            assert cli.warn_if_channel_unavailable(self._profile()) is True
        assert "not on PATH" in caplog.text

    def test_the_probe_matches_what_channel_py_actually_imports(self):
        # If channel.py's import changes and the probe does not, the preflight check starts
        # passing while the server still fails to start.
        source = (
            Path(__file__).resolve().parents[1]
            / "kiln" / "framework" / "mcp-server" / "channel.py"
        ).read_text(encoding="utf-8")
        for line in cli.CHANNEL_IMPORT_PROBE.strip().splitlines():
            statement = line.strip()
            if statement.startswith("from "):
                assert statement.split(" import ")[0] in source, f"probe drifted: {statement}"


class TestHostsPosixShell:
    """
    Regression: WezTerm is cross-platform, but the pane it hosts is not.

    `_hosts_posix_shell` used to be keyed on backend name alone (`backend == TMUX`), which
    meant WezTerm always got PowerShell-syntax commands -- correct on Windows, but on Linux
    or macOS a WezTerm pane runs bash/zsh and would receive `$env:VAR = '...'` syntax it
    can't parse. It's now keyed on the actual host OS for WezTerm specifically.
    """

    def test_tmux_is_always_posix(self, monkeypatch):
        monkeypatch.setattr(cli.os, "name", "nt")
        assert cli._hosts_posix_shell(cli.TMUX) is True

    def test_windows_terminal_is_never_posix(self, monkeypatch):
        # wt.exe only runs on Windows at all, so this never varies by host OS.
        monkeypatch.setattr(cli.os, "name", "posix")
        assert cli._hosts_posix_shell("wt") is False

    def test_wezterm_is_posix_on_linux_or_macos(self, monkeypatch):
        monkeypatch.setattr(cli.os, "name", "posix")
        assert cli._hosts_posix_shell(cli.WEZTERM) is True

    def test_wezterm_is_not_posix_on_windows(self, monkeypatch):
        monkeypatch.setattr(cli.os, "name", "nt")
        assert cli._hosts_posix_shell(cli.WEZTERM) is False

    def test_none_backend_follows_the_host_os(self, monkeypatch):
        # `none` prints the command instead of running it, and the README pairs it with
        # --dry-run as *the* way to see what a profile will do. It used to fall through to
        # the PowerShell branch on every platform, so on Linux the one backend whose whole
        # job is showing you the command showed `$env:VAR = '...'` that no shell there runs.
        monkeypatch.setattr(cli.os, "name", "posix")
        assert cli._hosts_posix_shell("none") is True

    def test_none_backend_is_powershell_on_windows(self, monkeypatch):
        monkeypatch.setattr(cli.os, "name", "nt")
        assert cli._hosts_posix_shell("none") is False


class TestStopMarkers:
    """
    Every python-backed pane must be stoppable — issue #18.

    `scheduler.inbox` and `scheduler.dashboard` were missing from the marker list, so both
    survived `kiln --stop` and kept polling the database after the swarm was supposedly
    down. This enumerates the pane types rather than restating the strings, so a future
    pane type cannot quietly reintroduce the gap.
    """

    #: Every scheduler value a profile can set that produces a python-backed pane.
    PANE_ROLES: ClassVar[list[dict]] = [
        {"role": "coder", "agent": "claude", "scheduler": "python", "mode": "auto"},
        {"role": "inbox", "scheduler": "inbox", "mode": "manual"},
        {"role": "dashboard", "scheduler": "dashboard", "mode": "manual"},
    ]

    @pytest.mark.parametrize("role_kwargs", PANE_ROLES)
    def test_each_python_backed_pane_has_a_stop_marker(self, tmp_path, role_kwargs):
        from launcher.commands import build_agent_command
        from launcher.config import RoleConfig
        from launcher.stop import KILN_PROCESS_MARKERS

        paths = KilnPaths.create(tmp_path / "proj", tmp_path / "fw")
        command = build_agent_command(RoleConfig(**role_kwargs), paths, branch="main")
        rendered = " ".join(command.argv)
        assert any(marker in rendered for marker in KILN_PROCESS_MARKERS), (
            f"{role_kwargs['role']} pane runs {rendered!r}, which no stop marker matches"
        )

    def test_the_capture_proxy_is_stoppable(self):
        # Not a pane -- a detached background process -- but started by the same launch and
        # it must end with it. A proxy left listening would relay whatever ran next.
        from launcher.stop import KILN_PROCESS_MARKERS

        assert "proxy.server" in KILN_PROCESS_MARKERS

    def test_the_mcp_channel_server_marker_survives(self):
        # Not produced by any pane command, so an enumeration test would happily delete it.
        from launcher.stop import KILN_PROCESS_MARKERS

        assert "channel.py" in KILN_PROCESS_MARKERS


class TestReclaimingLeftoverProxies:
    """
    Closing the terminal window is a normal way to end a swarm and it never reaches the
    proxy — that process is detached so it survives the launcher, and therefore the window.
    Left alone, every close would leak a listener and each launch would climb to the next
    port until `find_free_port` gave up and the launch failed.
    """

    def _processes(self, monkeypatch, rows):
        from launcher import stop

        monkeypatch.setattr(stop, "_windows_matches", lambda: rows)
        monkeypatch.setattr(stop, "_posix_matches", lambda: rows)

    def test_a_proxy_for_this_project_is_found(self, monkeypatch, tmp_path):
        from launcher import stop

        db = tmp_path / ".kiln" / "traffic.db"
        self._processes(monkeypatch, [
            (11, f"python -m proxy.server --db-path {db} --port 8787 --mode metadata"),
        ])
        assert [pid for pid, _ in stop.find_project_proxies(db)] == [11]

    def test_another_project_s_proxy_is_left_alone(self, monkeypatch, tmp_path):
        # Starting a swarm has no business killing another project's capture. `--stop` is
        # machine-wide by design; this is not.
        from launcher import stop

        mine = tmp_path / "mine" / "traffic.db"
        theirs = tmp_path / "theirs" / "traffic.db"
        self._processes(monkeypatch, [
            (22, f"python -m proxy.server --db-path {theirs} --port 8787 --mode metadata"),
        ])
        assert stop.find_project_proxies(mine) == []

    def test_other_kiln_processes_are_not_mistaken_for_proxies(self, monkeypatch, tmp_path):
        from launcher import stop

        db = tmp_path / ".kiln" / "traffic.db"
        self._processes(monkeypatch, [
            (33, f"python -m scheduler.dashboard --traffic-db {db}"),
        ])
        assert stop.find_project_proxies(db) == []

    def test_the_leftover_is_killed(self, monkeypatch, tmp_path):
        from launcher import stop

        db = tmp_path / ".kiln" / "traffic.db"
        self._processes(monkeypatch, [
            (44, f"python -m proxy.server --db-path {db} --port 8787 --mode metadata"),
        ])
        killed = []
        monkeypatch.setattr(stop, "kill_process", lambda pid: killed.append(pid) or True)
        assert stop.stop_project_proxies(db) == [44]
        assert killed == [44]

    def test_nothing_running_is_not_an_error(self, monkeypatch, tmp_path):
        from launcher import stop

        self._processes(monkeypatch, [])
        assert stop.stop_project_proxies(tmp_path / "traffic.db") == []


class TestProxyFlags:
    def test_the_proxy_is_on_by_default(self):
        # Flipped deliberately: metadata capture is cheap (~2.9KB a request) and it is what
        # the dashboard's prompt-weight panel reads.
        assert cli.build_parser().parse_args([]).proxy is True

    def test_it_can_be_turned_off(self):
        assert cli.build_parser().parse_args(["--no-proxy"]).proxy is False

    def test_capture_defaults_to_metadata(self):
        # The line that did NOT move. Bodies hold whatever source the agent read, in
        # plaintext, so they stay a second and deliberate opt-in even now that the proxy
        # itself runs unasked.
        assert cli.build_parser().parse_args([]).capture == "metadata"

    def test_full_capture_is_opt_in(self):
        args = cli.build_parser().parse_args(["--proxy", "--capture", "full"])
        assert args.proxy is True
        assert args.capture == "full"

    def test_the_port_is_configurable(self):
        assert cli.build_parser().parse_args(["--proxy-port", "9999"]).proxy_port == 9999

    def test_an_unknown_capture_mode_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["--capture", "everything"])


class TestProxyPortSelection:
    """
    A fixed port was survivable while the proxy was opt-in. Once it runs by default, two
    Kiln projects at once means the second proxy dies on bind — and its roles would still be
    pointed at the first project's proxy, which forwards fine and records their traffic into
    the wrong store. Nothing would surface that.
    """

    def test_the_preferred_port_is_used_when_free(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        assert cli.find_free_port(free) == free

    def test_a_taken_port_is_skipped(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            assert cli.find_free_port(port) > port

    def test_giving_up_is_an_error_not_a_random_port(self):
        # A swarm whose proxy landed somewhere unpredictable is harder to reason about than
        # one that refused to start.
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with pytest.raises(cli.LaunchError):
                cli.find_free_port(port, attempts=1)


class TestProxyReadiness:
    def test_a_listening_port_is_detected(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            assert cli.wait_until_listening(server.getsockname()[1], timeout=2.0) is True

    def test_a_dead_proxy_is_reported_rather_than_assumed_working(self):
        # Popen succeeding only means the process started; a failure to *bind* happens
        # inside the child and lands in its log, where the launch would never look.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        assert cli.wait_until_listening(port, timeout=0.5) is False


class TestProxyRoutes:
    """
    One proxy, several vendors. Roles -- not backends -- are what the path prefix
    identifies, so a mixed-backend swarm needs one route per non-default role rather than a
    second proxy on another port.
    """

    def _profile(self, *agents):
        from launcher.config import Profile, RoleConfig

        roles = [RoleConfig(role=f"r{index}", agent=agent)
                 for index, agent in enumerate(agents)]
        return Profile(name="p", description="", roles=roles, layout={})

    def test_a_claude_only_profile_needs_no_routes(self):
        # Anthropic is the proxy's default upstream; naming it again would be noise.
        assert cli.proxy_routes(self._profile("claude", "claude")) == []

    def test_a_codex_role_is_routed_to_its_own_upstream(self):
        assert cli.proxy_routes(self._profile("codex")) == [
            "--route=r0=chatgpt.com/backend-api/codex"
        ]

    def test_only_the_non_default_roles_are_named(self):
        routes = cli.proxy_routes(self._profile("claude", "codex", "claude"))
        assert routes == ["--route=r1=chatgpt.com/backend-api/codex"]

    def test_unroutable_backends_are_skipped(self):
        # copilot and grok have no verified override, so they run unproxied rather than
        # being pointed somewhere that would break their auth.
        assert cli.proxy_routes(self._profile("copilot", "grok")) == []


class TestCliParsing:
    def test_init_subcommand_is_recognised(self):
        args = cli.build_parser().parse_args(["init", "--working-dir", "x"])
        assert args.command == "init"

    def test_powershell_style_flags_are_accepted(self):
        # The shims forward arguments through unchanged.
        args = cli.build_parser().parse_args(["-WorkingDir", "x", "-Profile", "compact"])
        assert args.working_dir == "x"
        assert args.profile == "compact"

    @pytest.mark.parametrize("flag", ["-ProfileName", "-Profile", "--profile"])
    def test_every_documented_profile_spelling_works(self, flag):
        # `-ProfileName` was the PowerShell original's primary spelling and is what the README
        # documents. The port kept only the `-Profile` alias, silently breaking existing
        # invocations with an "unrecognized arguments" error.
        assert cli.build_parser().parse_args([flag, "compact"]).profile == "compact"

    @pytest.mark.parametrize("flag", ["-WorkingDir", "-Target", "--target", "--working-dir"])
    def test_every_documented_working_dir_spelling_works(self, flag):
        assert cli.build_parser().parse_args([flag, "x"]).working_dir == "x"

    @pytest.mark.parametrize(
        ("flag", "attribute"),
        [
            ("-Stop", "stop"), ("-Init", "init"), ("-NoGit", "no_git"),
            ("-ListProfiles", "list_profiles"), ("-Debug", "verbose"),
        ],
    )
    def test_every_powershell_switch_survives(self, flag, attribute):
        # Guards the whole original param block, not just the one that was found broken.
        assert getattr(cli.build_parser().parse_args([flag]), attribute) is True

    def test_unknown_positional_is_rejected(self, caplog):
        assert cli.main(["bogus"]) == 1

    def test_init_accepts_the_documented_bare_directory(self, monkeypatch):
        # `kiln init <dir>` is the form the README and kiln.sh's own usage block document,
        # but only one positional existed, so argparse rejected the directory outright and
        # the documented Unix scaffolding invocation could not run at all.
        seen = {}

        def fake_init(args):
            seen["working_dir"] = args.working_dir
            return 0

        monkeypatch.setattr(cli, "run_init", fake_init)
        assert cli.main(["init", "/tmp/new-project"]) == 0
        assert seen["working_dir"] == "/tmp/new-project"

    def test_bare_directory_beats_the_working_dir_default(self, monkeypatch):
        # Otherwise `init <dir>` would scaffold "." while naming another directory.
        seen = {}

        def fake_init(args):
            seen["working_dir"] = args.working_dir
            return 0

        monkeypatch.setattr(cli, "run_init", fake_init)
        cli.main(["init", "/tmp/explicit"])
        assert seen["working_dir"] == "/tmp/explicit"

    def test_stop_and_list_flags(self):
        assert cli.build_parser().parse_args(["--stop"]).stop is True
        assert cli.build_parser().parse_args(["--list-profiles"]).list_profiles is True

    def test_defaults_to_the_current_directory(self):
        assert cli.build_parser().parse_args([]).working_dir == "."


class TestCliErrors:
    def test_missing_working_directory_is_reported(self, tmp_path):
        assert cli.main(["--working-dir", str(tmp_path / "absent")]) == 1

    def test_missing_profile_is_reported(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(
            cli, "resolve_framework_root", lambda *a, **k: tmp_path / "no-framework"
        )
        assert cli.main(["--working-dir", str(project)]) == 1


class TestUnscaffoldedProject:
    """
    Regression: launching a project whose kiln/project/ is missing.

    Found by a live run against a real project whose kiln/ was never committed. It failed
    with a raw Python traceback from deep inside worker-file generation, naming only a path.
    """

    def test_missing_scaffolding_is_reported_with_a_remedy(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        with pytest.raises(TemplateError, match="not a scaffolded Kiln project"):
            check_project_scaffolding(paths)

    def test_error_names_the_init_command(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        with pytest.raises(TemplateError, match="kiln init"):
            check_project_scaffolding(paths)

    def test_missing_workflow_is_fatal(self, tmp_path):
        from launcher.templates import TemplateError, check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        with pytest.raises(TemplateError, match="handoff routing"):
            check_project_scaffolding(paths)

    def test_workflow_alone_is_enough_to_proceed(self, tmp_path):
        from launcher.templates import check_project_scaffolding

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        paths.workflow_md.write_text("# Workflow\n", encoding="utf-8")
        check_project_scaffolding(paths)  # must not raise

    def test_optional_sections_degrade_instead_of_aborting(self, tmp_path):
        from launcher.templates import read_constitution

        paths = KilnPaths.create(tmp_path, tmp_path)
        paths.constitution_dir.mkdir(parents=True)
        assert read_constitution(paths, "project") == ""
        assert read_constitution(paths, "engineering") == ""

    def test_cli_reports_cleanly_instead_of_a_traceback(self, tmp_path, framework, monkeypatch):
        # The launcher must never surface a Python traceback for a user-fixable condition.
        monkeypatch.setattr(cli, "resolve_framework_root", lambda *a, **k: framework)
        project = tmp_path / "unscaffolded"
        project.mkdir()
        (project / "kiln.profiles.json").write_text(
            json.dumps({"profiles": {"p": {"terminals": [{"role": "coder"}]}}, "default": "p"}),
            encoding="utf-8",
        )
        assert cli.main(["--working-dir", str(project), "--terminal", "none"]) == 1


class TestCliInit:
    def test_scaffolds_through_the_cli(self, tmp_path, framework, monkeypatch):
        monkeypatch.setattr(cli, "resolve_framework_root", lambda *a, **k: framework)
        target = tmp_path / "viacli"
        assert cli.main(["init", "--working-dir", str(target)]) == 0
        assert (target / "kiln" / "project" / "constitution" / "workflow.md").is_file()
