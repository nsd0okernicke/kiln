"""
Terminal backends.

Everything here is tested through `dry_run` or the pure builders — no test spawns a real
terminal. The WezTerm assertions matter most: its Lua reads state from environment
variables, and a missing one fails silently rather than erroring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln.launcher.infrastructure.terminals import (
    HERDR,
    NONE,
    TMUX,
    WEZTERM,
    WINDOWS_TERMINAL,
    PaneSpec,
    TerminalError,
    detect_backend,
    herdr,
    launch,
    tmux,
    wezterm,
    windows_terminal,
)

PANES = [
    PaneSpec(role="specifier", name="Specifier", path="C:/proj", cmd="claude x", mode="manual"),
    PaneSpec(role="coder", name="Coder", path="C:/proj/.worktrees/coder", cmd="claude y"),
]

GRID_LAYOUT = {
    "tabs": [
        {
            "title": "All Roles",
            "gridRows": 2,
            "gridCols": 2,
            "panes": [{"role": "specifier"}, {"role": "coder"}],
        }
    ]
}

FOUR_PANE_GRID = {
    "tabs": [
        {
            "title": "All Roles",
            "gridRows": 2,
            "gridCols": 2,
            "panes": [
                {"role": "specifier"},
                {"role": "coder"},
                {"role": "reviewer"},
                {"role": "architect"},
            ],
        }
    ]
}

FOUR_PANES = [
    *PANES,
    PaneSpec(role="reviewer", name="Reviewer", path="C:/p/.worktrees/reviewer", cmd="pi r"),
    PaneSpec(role="architect", name="Architect", path="C:/p/.worktrees/architect", cmd="pi a"),
]


class TestBackendDetection:
    def test_explicit_request_wins(self):
        assert detect_backend("WezTerm", env={}) == "wezterm"

    def test_environment_variable_is_honoured(self):
        assert detect_backend(None, env={"KILN_TERMINAL": "tmux"}) == TMUX

    def test_request_beats_environment(self):
        assert detect_backend("tmux", env={"KILN_TERMINAL": "wt"}) == TMUX

    def test_running_inside_wezterm_reuses_it_when_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "wezterm" if name == "wezterm" else None)
        assert detect_backend(env={"WEZTERM_PANE": "3"}) == WEZTERM

    def test_herdr_is_preferred_over_wezterm_when_installed(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: {"herdr": "herdr", "wezterm": "wezterm"}.get(name)
        )
        assert detect_backend(env={}) == HERDR

    def test_installed_wezterm_is_preferred_outside_wezterm(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "wezterm" if name == "wezterm" else None)
        assert detect_backend(env={}) == WEZTERM

    def test_windows_terminal_is_the_windows_fallback(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(os, "name", "nt")
        assert detect_backend(env={}) == WINDOWS_TERMINAL

    def test_tmux_is_the_posix_fallback_when_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "tmux" if name == "tmux" else None)
        monkeypatch.setattr(os, "name", "posix")
        assert detect_backend(env={}) == TMUX

    def test_running_inside_herdr_prefers_herdr_backend(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert detect_backend(env={"HERDR_ENV": "1"}) == HERDR

    def test_running_inside_herdr_beats_kiln_terminal(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        # The KILN_TERMINAL env var is checked BEFORE running-inside detection,
        # so explicit config still wins.
        assert detect_backend("tmux", env={"HERDR_ENV": "1"}) == TMUX

    def test_herdr_env_not_set_falls_through(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(os, "name", "posix")
        assert detect_backend(env={"HERDR_ENV": "0"}) == NONE

    def test_no_supported_binary_selects_log_only_backend(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(os, "name", "posix")
        assert detect_backend(env={}) == NONE


class TestWezTermEnvironment:
    def test_exports_project_dir(self):
        # The PowerShell original never set this, so the Lua's update-status handler
        # returned immediately and the live status bar never rendered.
        env = wezterm.build_environment(PANES, {}, Path("C:/proj"))
        assert env[wezterm.ENV_PROJECT_DIR] == "C:/proj"

    def test_project_dir_uses_forward_slashes(self):
        # Lua concatenates it with '/.kiln/...'; backslashes would break the path.
        env = wezterm.build_environment(PANES, {}, Path("C:/a/b"))
        assert "\\" not in env[wezterm.ENV_PROJECT_DIR]

    def test_roles_json_carries_everything_the_lua_reads(self):
        env = wezterm.build_environment(PANES, {}, Path("C:/proj"))
        roles = json.loads(env[wezterm.ENV_ROLES])
        assert [r["role"] for r in roles] == ["specifier", "coder"]
        for role in roles:
            assert set(role) >= {"role", "name", "path", "cmd", "mode", "passive"}

    def test_roles_json_marks_stateless_panes(self):
        # The Lua cannot work this out for itself -- it never sees a profile -- so the flag
        # has to ride along with the pane it describes.
        panes = [
            *PANES,
            PaneSpec(
                role="cockpit",
                name="Cockpit",
                path="C:/proj",
                cmd="python -m kiln.cockpit.infrastructure.http.server",
                mode="manual",
                passive=True,
            ),
        ]

        roles = json.loads(wezterm.build_environment(panes, {}, Path("C:/p"))[wezterm.ENV_ROLES])

        assert {r["role"]: r["passive"] for r in roles} == {
            "specifier": False,
            "coder": False,
            "cockpit": True,
        }

    def test_layout_is_omitted_when_absent(self):
        assert wezterm.ENV_LAYOUT not in wezterm.build_environment(PANES, {}, Path("C:/p"))

    def test_layout_is_serialised_when_present(self):
        env = wezterm.build_environment(PANES, GRID_LAYOUT, Path("C:/p"))
        assert json.loads(env[wezterm.ENV_LAYOUT])["tabs"][0]["gridRows"] == 2

    def test_dry_run_spawns_nothing(self):
        assert wezterm.launch(PANES, {}, Path("C:/p"), dry_run=True) == ["wezterm", "start"]

    def test_state_colours_come_from_the_shared_scheduler_table(self):
        # Not a hand-copied second palette -- scheduler.pane_status.STATE_COLORS_HEX is the
        # only place these values are written, so the pane's own status bar and this badge
        # cannot disagree on what a given state looks like.
        from kiln.scheduler.infrastructure.terminal.pane_status import STATE_COLORS_HEX

        env = wezterm.build_environment(PANES, {}, Path("C:/proj"))
        assert json.loads(env[wezterm.ENV_STATE_COLORS]) == STATE_COLORS_HEX

    def test_missing_binary_is_reported(self, monkeypatch):
        monkeypatch.setattr(wezterm.shutil, "which", lambda name: None)
        with pytest.raises(TerminalError, match="wezterm not found"):
            wezterm.launch(PANES, {}, Path("C:/p"))

    def test_existing_user_config_is_restored_after_launch(self, tmp_path, monkeypatch):
        target = tmp_path / ".wezterm.lua"
        target.write_text("return user_config\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(wezterm, "config_path", lambda: target)
        monkeypatch.setattr(wezterm.shutil, "which", lambda name: "wezterm")
        monkeypatch.setattr(wezterm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            wezterm.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs))
        )

        assert wezterm.launch(PANES, GRID_LAYOUT, tmp_path) == ["wezterm", "start"]
        assert target.read_text(encoding="utf-8") == "return user_config\n"
        assert calls[0][1]["env"][wezterm.ENV_PROJECT_DIR] == tmp_path.as_posix()

    def test_generated_config_is_removed_when_user_had_none(self, tmp_path, monkeypatch):
        target = tmp_path / ".wezterm.lua"
        monkeypatch.setattr(wezterm, "config_path", lambda: target)
        monkeypatch.setattr(wezterm.shutil, "which", lambda name: "wezterm")
        monkeypatch.setattr(wezterm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(wezterm.subprocess, "run", lambda *args, **kwargs: None)

        wezterm.launch(PANES, {}, tmp_path)

        assert not target.exists()

    def test_user_config_is_restored_even_when_start_raises(self, tmp_path, monkeypatch):
        target = tmp_path / ".wezterm.lua"
        target.write_text("user config", encoding="utf-8")
        monkeypatch.setattr(wezterm, "config_path", lambda: target)
        monkeypatch.setattr(wezterm.shutil, "which", lambda name: "wezterm")
        monkeypatch.setattr(wezterm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            wezterm.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("start failed")),
        )

        with pytest.raises(OSError, match="start failed"):
            wezterm.launch(PANES, {}, tmp_path)
        assert target.read_text(encoding="utf-8") == "user config"


class TestWezTermLua:
    def test_reads_the_environment_variables_the_launcher_sets(self):
        for name in (
            wezterm.ENV_ROLES,
            wezterm.ENV_LAYOUT,
            wezterm.ENV_PROJECT_DIR,
            wezterm.ENV_STATE_COLORS,
        ):
            assert f"os.getenv('{name}')" in wezterm.LUA_CONFIG

    def test_uses_lowercase_kiln_for_pane_ids(self):
        # The original wrote '.Kiln', which only worked by accident on Windows.
        assert "'/.kiln/pane-ids.tsv'" in wezterm.LUA_CONFIG
        assert ".Kiln/" not in wezterm.LUA_CONFIG

    def test_reads_status_from_the_json_file_not_the_pane_title(self):
        # The agent rewrites its own OSC-0 title constantly and would win the race.
        assert "/.kiln/status/" in wezterm.LUA_CONFIG

    def test_stateless_panes_get_no_status_badge(self):
        # They never write a status file, and the `mode == 'manual'` fallback below badges a
        # role with no status as `waiting` -- so a healthy cockpit advertised itself as
        # waiting for something. Filtered into `shown` before the loop.
        assert "if not r.passive then" in wezterm.LUA_CONFIG

    def test_the_badge_separator_counts_the_filtered_list(self):
        # `i < #roles` would emit a trailing separator whenever the last role is a hidden
        # pane -- which, in the shipped `full` profile, it always is.
        assert "if i < #shown then" in wezterm.LUA_CONFIG
        assert "if i < #roles then" not in wezterm.LUA_CONFIG

    def test_hidden_panes_are_still_spawned(self):
        # Only the badge row filters: `gui-startup` must create every pane, or the cockpit
        # would have no process at all.
        startup = wezterm.LUA_CONFIG.partition("wezterm.on('gui-startup'")[2]

        assert "passive" not in startup

    def test_defines_colours_for_scheduler_states(self):
        # The scheduler reports states the old wrapper never did. Colours now live in
        # scheduler.pane_status.STATE_COLORS_HEX (see test_state_colours_come_from_the_
        # shared_scheduler_table), not as Lua literals -- this just confirms the Lua reads
        # them via the env var rather than falling back to STATE_COLOR_DEFAULT for every role.
        from kiln.scheduler.infrastructure.terminal.pane_status import STATE_COLORS_HEX

        for state in ("retrying", "blocked", "idle"):
            assert state in STATE_COLORS_HEX
        assert "STATE_COLORS = wezterm.json_parse" in wezterm.LUA_CONFIG

    def test_ctrl_c_copies_when_text_is_selected(self):
        # Without this, selecting scheduler output and pressing Ctrl+C sends SIGINT and
        # kills the scheduler instead of copying.
        assert "get_selection_text_for_pane" in wezterm.LUA_CONFIG
        assert "CopyTo" in wezterm.LUA_CONFIG

    def test_ctrl_c_still_interrupts_with_no_selection(self):
        # Stopping a runaway agent must keep working.
        assert "SendKey { key = 'c', mods = 'CTRL' }" in wezterm.LUA_CONFIG

    def test_ctrl_v_pastes(self):
        assert "key = 'v'" in wezterm.LUA_CONFIG

    def test_the_grid_branch_is_only_taken_when_a_grid_was_asked_for(self):
        """
        Regression: the inbox pane came up on the right instead of the bottom.

        `grid_cols` defaulted to `#tab_def.panes`, so the `grid_cols > 1` test was true for
        *any* two-pane tab. Such a tab fell into the grid branch, whose split direction is
        hardcoded to 'Right', silently overriding the per-pane `direction` honoured in the
        simple branch. The condition must test what the tab declared, not how many panes it
        happens to have.
        """
        assert "if tab_def.gridRows or tab_def.gridCols then" in wezterm.LUA_CONFIG
        assert "if grid_rows > 1 or grid_cols > 1 then" not in wezterm.LUA_CONFIG

    def test_per_pane_direction_and_size_are_honoured(self):
        # Without these the inbox cannot be a bottom strip.
        assert "pane_def.direction or 'Right'" in wezterm.LUA_CONFIG
        assert "pane_def.size or" in wezterm.LUA_CONFIG

    def test_layout_extras_survive_serialisation_to_the_lua(self):
        # The Lua can only honour keys that actually reach it in KILN_LAYOUT_JSON.
        layout = {
            "tabs": [
                {
                    "title": "Human",
                    "panes": [
                        {"role": "specifier"},
                        {"role": "coder", "direction": "Bottom", "size": 0.22},
                    ],
                }
            ]
        }
        env = wezterm.build_environment(PANES, layout, Path("C:/p"))
        pane = json.loads(env[wezterm.ENV_LAYOUT])["tabs"][0]["panes"][1]
        assert pane["direction"] == "Bottom"
        assert pane["size"] == 0.22

    def test_only_forces_pwsh_on_windows(self):
        # A hardcoded pwsh.exe default_prog would break the Unix path.
        assert "wezterm.target_triple:find('windows')" in wezterm.LUA_CONFIG


class TestWindowsTerminal:
    def test_default_layout_is_one_tab_per_role(self):
        args = windows_terminal.build_layout(PANES, None)
        assert args.count("new-tab") == 2
        assert "split-pane" not in args

    def test_first_tab_has_no_leading_separator(self):
        assert windows_terminal.build_layout(PANES, None)[0] == "new-tab"

    def test_each_tab_sets_directory_and_command(self):
        args = windows_terminal.build_layout(PANES, None)
        assert "C:/proj" in args
        assert "claude x" in args

    def test_layout_panes_become_splits(self):
        args = windows_terminal.build_layout(PANES, GRID_LAYOUT)
        assert args.count("new-tab") == 1
        assert args.count("split-pane") == 1

    def test_explicit_tab_title_is_used(self):
        args = windows_terminal.build_layout(PANES, GRID_LAYOUT)
        assert "All Roles" in args

    def test_unknown_roles_in_layout_are_skipped(self):
        layout = {"tabs": [{"panes": [{"role": "ghost"}]}]}
        # Falls back rather than launching an empty window.
        assert windows_terminal.build_layout(PANES, layout).count("new-tab") == 2

    def test_dry_run_returns_the_command(self):
        command = windows_terminal.launch(PANES, None, dry_run=True)
        assert command[0] == "wt.exe"

    def test_launch_requires_windows_terminal(self, monkeypatch):
        monkeypatch.setattr(windows_terminal.shutil, "which", lambda _name: None)
        with pytest.raises(TerminalError, match="not found on PATH"):
            windows_terminal.launch(PANES, None)

    def test_launch_starts_windows_terminal(self, monkeypatch):
        launched = []
        monkeypatch.setattr(windows_terminal.shutil, "which", lambda _name: "wt.exe")
        monkeypatch.setattr(windows_terminal.subprocess, "Popen", launched.append)
        command = windows_terminal.launch(PANES, None)
        assert launched == [command]

    def test_multiple_defined_tabs_have_a_separator(self):
        layout = {
            "tabs": [
                {"panes": [{"role": "specifier"}]},
                {"panes": [{"role": "coder"}]},
            ]
        }
        assert windows_terminal.SEPARATOR in windows_terminal.build_layout(PANES, layout)


class TestTmux:
    def setup_method(self):
        tmux._project_name = ""

    def test_session_name_is_role_scoped(self):
        assert tmux.session_name("coder") == "kiln-coder"

    def test_session_name_is_project_scoped_when_set(self):
        # Trigger project-scoped naming.
        tmux.launch([], None, project_dir=Path("my-project"), dry_run=True)
        assert tmux.session_name("coder") == "kiln-my-project-coder"

    def test_creates_detached_session_then_sends_the_command(self):
        commands = tmux.build_session_commands(PANES[1])
        assert commands[0][:4] == ["tmux", "new-session", "-d", "-s"]
        assert commands[-1][1] == "send-keys"
        assert commands[-1][-2] == "claude y"

    def test_session_starts_in_the_roles_worktree(self):
        commands = tmux.build_session_commands(PANES[1])
        assert "C:/proj/.worktrees/coder" in commands[0]

    def test_window_is_named_for_the_role(self):
        assert "Coder" in tmux.build_session_commands(PANES[1])[1]

    def test_dry_run_plans_every_role(self):
        planned = tmux.launch(PANES, None, dry_run=True)
        assert any("kiln-specifier" in line for line in planned)
        assert any("kiln-coder" in line for line in planned)

    def test_missing_binary_is_reported(self, monkeypatch):
        monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
        with pytest.raises(TerminalError, match="tmux not found"):
            tmux.launch(PANES, None)

    def test_existing_session_is_left_untouched(self, monkeypatch):
        monkeypatch.setattr(tmux.shutil, "which", lambda name: "tmux")
        monkeypatch.setattr(tmux, "session_exists", lambda role: True)
        monkeypatch.setattr(tmux, "_run", lambda command: pytest.fail("ran tmux command"))

        assert tmux.launch(PANES, None, dry_run=False) == []

    def test_project_dir_scopes_session_names(self, monkeypatch):
        monkeypatch.setattr(tmux.shutil, "which", lambda name: "tmux")
        monkeypatch.setattr(tmux, "session_exists", lambda role: False)
        calls = []
        def _record(command):
            calls.append(command)
            return type("R", (), {"returncode": 0, "stderr": ""})()
        monkeypatch.setattr(tmux, "_run", _record)

        tmux.launch(PANES[:1], None, project_dir=Path("my-project"))

        # Session name should include project dir name.
        assert any("kiln-my-project-specifier" in c[4] for c in calls)

    def test_launch_runs_each_planned_command(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tmux.shutil, "which", lambda name: "tmux")
        monkeypatch.setattr(tmux, "session_exists", lambda role: False)
        monkeypatch.setattr(
            tmux,
            "_run",
            lambda command: calls.append(command) or SimpleNamespace(returncode=0, stderr=""),
        )

        planned = tmux.launch(PANES[:1], None)

        assert len(calls) == 3
        assert planned == [" ".join(command) for command in calls]

    def test_failed_command_stops_setting_up_only_that_role(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tmux.shutil, "which", lambda name: "tmux")
        monkeypatch.setattr(tmux, "session_exists", lambda role: False)
        monkeypatch.setattr(
            tmux,
            "_run",
            lambda command: calls.append(command) or SimpleNamespace(returncode=1, stderr="broken"),
        )

        tmux.launch(PANES[:1], None)

        assert len(calls) == 1


class TestHerdr:
    def test_workspace_label(self):
        assert herdr.workspace_label(Path("my-project")) == "kiln-my-project"
        assert herdr.workspace_label(Path("C:/Users/me/proj")) == "kiln-proj"

    def test_pane_id_format(self):
        assert herdr.pane_id("wN", 1) == "wN:p1"
        assert herdr.pane_id("wN", 5) == "wN:p5"

    def test_tab_members(self):
        tab_def = {"panes": [{"role": "specifier"}, {"role": "coder"}]}
        assert [m.role for m in herdr._tab_members(PANES, tab_def)] == ["specifier", "coder"]

    def test_tab_members_skips_unknown(self):
        assert herdr._tab_members(PANES, {"panes": [{"role": "ghost"}]}) == []

    def test_tab_title(self):
        assert herdr._tab_title({"title": "My Tab"}, PANES[:1]) == "My Tab"
        assert herdr._tab_title({}, PANES) == "Specifier & Coder"

    def test_dry_run_plans_workspace_and_panes(self):
        planned = herdr.launch(PANES, None, Path("proj"), dry_run=True)
        assert any("workspace create" in line for line in planned)
        assert any("pane run" in line for line in planned)

    def test_dry_run_workspace_label(self):
        planned = herdr.launch(PANES, {}, Path("my-project"), dry_run=True)
        assert any("kiln-my-project" in line for line in planned)

    def test_dry_run_with_grid(self):
        planned = herdr.launch(PANES, GRID_LAYOUT, Path("p"), dry_run=True)
        assert any("pane split" in line for line in planned)
        assert any("pane run" in line for line in planned)

    def test_missing_binary(self, monkeypatch):
        monkeypatch.setattr(herdr.shutil, "which", lambda n: None)
        with pytest.raises(TerminalError, match="herdr not found"):
            herdr.launch(PANES, None, Path("p"))

    def test_dry_run_no_real_calls(self, monkeypatch):
        def _raise(*a, **kw):
            raise RuntimeError("no")
        monkeypatch.setattr(herdr, "_run", _raise)
        monkeypatch.setattr(herdr, "_run_in_ws", _raise)
        assert herdr.launch(PANES, {}, Path("p"), dry_run=True)

    def test_no_layout_both_roles(self):
        planned = herdr.launch(PANES, None, Path("p"), dry_run=True)
        reports = [line for line in planned if "report-agent" in line]
        assert len(reports) == 2
        assert any("kiln-specifier" in line for line in reports)
        assert any("kiln-coder" in line for line in reports)

    def test_grid_2x2_3_splits(self):
        planned = herdr.launch(FOUR_PANES, FOUR_PANE_GRID, Path("p"), dry_run=True)
        assert len([line for line in planned if "pane split" in line]) == 3
        assert len([line for line in planned if "pane run" in line]) == 4
        assert len([line for line in planned if "report-agent" in line]) == 4

    def test_grid_2x2_2_panes_1_split(self):
        planned = herdr.launch(PANES, GRID_LAYOUT, Path("p"), dry_run=True)
        assert len([line for line in planned if "pane split" in line]) == 1
        assert len([line for line in planned if "pane run" in line]) == 2

    def test_linear_down_split(self):
        layout = {"tabs": [{"panes": [{"role": "specifier"}, {"role": "coder"}]}]}
        planned = herdr.launch(PANES, layout, Path("p"), dry_run=True)
        assert any("down" in line for line in planned if "pane split" in line)

    def test_kiln_state_mapping(self):
        assert len(herdr.KILN_STATE_TO_HERDR) == 14
        for s in ("blocked", "escalated", "halted"):
            assert herdr.KILN_STATE_TO_HERDR[s] == "blocked"
        for s in ("handoff", "handing-off"):
            assert herdr.KILN_STATE_TO_HERDR[s] == "done"
        for s in ("idle", "waiting"):
            assert herdr.KILN_STATE_TO_HERDR[s] == "idle"
        working = ("starting", "receiving", "working", "delegating",
                   "verifying", "approval", "retrying")
        for s in working:
            assert herdr.KILN_STATE_TO_HERDR[s] == "working"

    def test_herdr_env_var(self):
        from kiln.launcher.infrastructure.terminals import HERDR_ENV_VAR, WEZTERM_PANE_VAR
        assert HERDR_ENV_VAR == "HERDR_ENV"
        assert WEZTERM_PANE_VAR == "WEZTERM_PANE"

    def test_ws_id_from_create(self):
        assert herdr._ws_id_from_create('{"result":{"workspace":{"workspace_id":"wN"}}}') == "wN"
        assert herdr._ws_id_from_create("") == ""

    def test_find_json_error(self):
        assert "not_found" in herdr._find_json_error(
            '{"error":{"code":"not_found","message":"pane not found"}}')
        assert herdr._find_json_error('{"result":{"ok":true}}') == ""
        assert herdr._find_json_error("") == ""

    def test_open_herdr_tui_skipped_in_dry_run(self, monkeypatch):
        def _fail(*a, **kw):
            pytest.fail("called subprocess.run")
        monkeypatch.setattr(herdr.subprocess, "run", _fail)
        herdr._open_herdr_tui(dry_run=True)

    def test_open_herdr_tui_calls_run(self, monkeypatch):
        run_calls = []
        monkeypatch.setattr(herdr.subprocess, "run", lambda *a, **kw: run_calls.append((a, kw)))
        herdr._open_herdr_tui(dry_run=False)
        assert len(run_calls) == 1
        args, kwargs = run_calls[0]
        assert args[0] == ["herdr"]
        assert kwargs.get("check") is False

    def test_open_herdr_tui_swallows_errors(self, monkeypatch):
        def _throw(*a, **kw):
            raise RuntimeError("bang")
        monkeypatch.setattr(herdr.subprocess, "run", _throw)
        monkeypatch.setattr(herdr.log, "warning", lambda *a, **kw: None)
        herdr._open_herdr_tui(dry_run=False)  # Should not raise

    # ------------------------------------------------------------------ #
    # Non-dry-run & error path coverage
    # ------------------------------------------------------------------ #

    def test_create_workspace_raises_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            herdr, "_run",
            lambda *a: SimpleNamespace(
                returncode=1, stdout='', stderr='connection refused'
            ),
        )
        with pytest.raises(TerminalError, match="failed to create workspace"):
            herdr._create_workspace("test", Path("p"), [], dry_run=False)

    def test_create_workspace_raises_on_json_error(self, monkeypatch):
        monkeypatch.setattr(
            herdr, "_run",
            lambda *a: SimpleNamespace(
                returncode=0,
                stdout='{"error":{"code":"bad","message":"nope"}}',
                stderr='',
            ),
        )
        with pytest.raises(TerminalError, match="nope"):
            herdr._create_workspace("test", Path("p"), [], dry_run=False)

    def test_create_workspace_raises_on_missing_id(self, monkeypatch):
        monkeypatch.setattr(
            herdr, "_run",
            lambda *a: SimpleNamespace(
                returncode=0,
                stdout='{"result":{"workspace":{"label":"x"}}}',
                stderr='',
            ),
        )
        with pytest.raises(TerminalError, match="No workspace_id"):
            herdr._create_workspace("test", Path("p"), [], dry_run=False)

    def test_non_dry_run_creates_workspace_and_calls_all_herdr(self, monkeypatch):
        """End-to-end non-dry-run: mock _run/_run_in_ws for workspace create + pane ops."""
        calls = []

        def fake_run(args: list[str], **kw):
            calls.append(("run", args, kw))
            return SimpleNamespace(
                returncode=0,
                stdout='{"result":{"workspace":{"workspace_id":"wX"}}}',
                stderr='',
            )

        def fake_run_in_ws(args: list[str], ws_id: str):
            calls.append(("run_in_ws", args, ws_id))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        monkeypatch.setattr(herdr, "_run", fake_run)
        monkeypatch.setattr(herdr, "_run_in_ws", fake_run_in_ws)
        # Prevent _open_herdr_tui from launching real herdr
        monkeypatch.setattr(herdr, "_open_herdr_tui", lambda dr: None)

        layout = {
            "tabs": [
                {"panes": [{"role": "specifier"}, {"role": "coder"}]},
                {
                    "title": "Grid Tab",
                    "gridRows": 2,
                    "gridCols": 2,
                    "panes": [
                        {"role": "specifier"},
                        {"role": "reviewer"},
                        {"role": "coder"},
                        {"role": "architect"},
                    ],
                },
            ]
        }
        panes = [
            PaneSpec(role="specifier", name="Spec", path="/p", cmd="pi s"),
            PaneSpec(role="coder", name="Coder", path="/p", cmd="pi c"),
            PaneSpec(role="reviewer", name="Reviewer", path="/p", cmd="pi r"),
            PaneSpec(role="architect", name="Arch", path="/p", cmd="pi a"),
        ]

        result = herdr.launch(panes, layout, Path("proj"), dry_run=False)

        # Should have planned commands AND executed them via _run / _run_in_ws
        assert result
        assert any("workspace create" in c for c in result)
        assert any("pane run" in c for c in result)
        assert any("report-agent" in c for c in result)
        # _run called at least for workspace create
        run_calls = [c for c in calls if c[0] == "run"]
        assert len(run_calls) >= 1
        # _run_in_ws called for tab create, pane run, report-agent, splits
        ws_calls = [c for c in calls if c[0] == "run_in_ws"]
        assert len(ws_calls) >= 5

    def test_non_dry_run_flat_layout(self, monkeypatch):
        """No layout: one tab per role (tests _new_tab_with_pane)."""
        calls = []

        def fake_run(args, **kw):
            calls.append(("run", args))
            return SimpleNamespace(
                returncode=0,
                stdout='{"result":{"workspace":{"workspace_id":"wX"}}}',
                stderr='',
            )

        def fake_run_in_ws(args, ws_id):
            calls.append(("run_in_ws", args, ws_id))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        monkeypatch.setattr(herdr.shutil, "which", lambda n: "herdr" if n == "herdr" else None)
        monkeypatch.setattr(herdr, "_run", fake_run)
        monkeypatch.setattr(herdr, "_run_in_ws", fake_run_in_ws)
        monkeypatch.setattr(herdr, "_open_herdr_tui", lambda dr: None)

        planned = herdr.launch(PANES, None, Path("proj"), dry_run=False)
        # Should have created 2nd tab via tab create
        tab_creates = [line for line in planned if "tab create" in line]
        assert len(tab_creates) == 1

    def test_rename_tab_empty_title_returns_early(self, monkeypatch):
        calls = []
        monkeypatch.setattr(herdr, "_run_in_ws", lambda *a: calls.append(a))
        herdr._rename_tab("wX", "", [], dry_run=False)
        assert len(calls) == 0

    def test_rename_tab_logs_failure(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            herdr, "_run_in_ws",
            lambda *a: SimpleNamespace(returncode=1, stderr="not found"),
        )
        monkeypatch.setattr(herdr.log, "warning", lambda *a, **kw: warnings.append(a))
        herdr._rename_tab("wX", "My Tab", [], dry_run=False)
        assert len(warnings) >= 1

    def test_linear_panes_empty_members(self):
        pc = herdr._linear_panes("wX", 5, [], [], dry_run=True)
        assert pc == 5

    def test_grid_panes_empty_members(self):
        pc = herdr._grid_panes("wX", 5, [], 2, 2, [], dry_run=True)
        assert pc == 5

    def test_new_tab_with_pane_named(self):
        planned = []
        pane = PaneSpec(role="coder", name="Coder", path="/p", cmd="pi")
        pc = herdr._new_tab_with_pane("wX", 3, pane, planned, dry_run=True)
        assert pc == 4
        assert any("Coder" in line for line in planned)
        assert any("report-agent" in line for line in planned)

    def test_new_tab_with_pane_unnamed(self):
        planned = []
        pane = PaneSpec(role="coder", name="", path="/p", cmd="pi")
        pc = herdr._new_tab_with_pane("wX", 3, pane, planned, dry_run=True)
        assert pc == 4
        assert not any("--label" in line for line in planned)

    def test_find_json_error_parse_exception(self):
        """Non-JSON input triggers except and returns empty string."""
        assert herdr._find_json_error("{{{not json") == ""
        assert herdr._find_json_error("None") == ""

    def test_ws_id_from_create_parse_exception(self):
        assert herdr._ws_id_from_create("{{{not json") == ""

    def test_do_split_non_dry_run_logs_failure(self, monkeypatch):
        errors = []
        monkeypatch.setattr(
            herdr, "_run_in_ws",
            lambda *a: SimpleNamespace(returncode=1, stderr="no pane"),
        )
        monkeypatch.setattr(herdr.log, "error", lambda *a, **kw: errors.append(a))
        herdr._do_split("wX", "wX:p1", "down", [], dry_run=False)
        assert len(errors) >= 1

    def test_run_in_pane_non_dry_run_logs_agent_failure(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            herdr, "_run_in_ws",
            lambda *a: SimpleNamespace(returncode=1, stderr="agent fail"),
        )
        monkeypatch.setattr(herdr.log, "warning", lambda *a, **kw: warnings.append(a))
        pane = PaneSpec(role="coder", name="", path="/p", cmd="pi")
        herdr._run_in_pane("wX", "wX:p1", pane, [], dry_run=False)
        assert len(warnings) >= 1


class TestDispatch:
    @pytest.mark.parametrize("backend", [WEZTERM, WINDOWS_TERMINAL, TMUX, HERDR])
    def test_dry_run_reaches_each_backend(self, backend):
        assert launch(backend, PANES, {}, Path("C:/p"), dry_run=True)

    def test_none_backend_only_logs(self):
        assert launch(NONE, PANES, {}, Path("C:/p")) == []

    def test_unknown_backend_raises(self):
        with pytest.raises(TerminalError, match="unknown terminal backend"):
            launch("kitty", PANES, {}, Path("C:/p"), dry_run=True)
