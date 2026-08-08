"""
Terminal backends.

Everything here is tested through `dry_run` or the pure builders — no test spawns a real
terminal. The WezTerm assertions matter most: its Lua reads state from environment
variables, and a missing one fails silently rather than erroring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from launcher.terminals import (
    NONE,
    TMUX,
    WEZTERM,
    WINDOWS_TERMINAL,
    PaneSpec,
    TerminalError,
    detect_backend,
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


class TestBackendDetection:
    def test_explicit_request_wins(self):
        assert detect_backend("WezTerm", env={}) == "wezterm"

    def test_environment_variable_is_honoured(self):
        assert detect_backend(None, env={"KILN_TERMINAL": "tmux"}) == TMUX

    def test_request_beats_environment(self):
        assert detect_backend("tmux", env={"KILN_TERMINAL": "wt"}) == TMUX


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
            assert set(role) >= {"role", "name", "path", "cmd", "mode"}

    def test_layout_is_omitted_when_absent(self):
        assert wezterm.ENV_LAYOUT not in wezterm.build_environment(PANES, {}, Path("C:/p"))

    def test_layout_is_serialised_when_present(self):
        env = wezterm.build_environment(PANES, GRID_LAYOUT, Path("C:/p"))
        assert json.loads(env[wezterm.ENV_LAYOUT])["tabs"][0]["gridRows"] == 2

    def test_dry_run_spawns_nothing(self):
        assert wezterm.launch(PANES, {}, Path("C:/p"), dry_run=True) == ["wezterm", "start"]


class TestWezTermLua:
    def test_reads_the_environment_variables_the_launcher_sets(self):
        for name in (wezterm.ENV_ROLES, wezterm.ENV_LAYOUT, wezterm.ENV_PROJECT_DIR):
            assert f"os.getenv('{name}')" in wezterm.LUA_CONFIG

    def test_uses_lowercase_kiln_for_pane_ids(self):
        # The original wrote '.Kiln', which only worked by accident on Windows.
        assert "'/.kiln/pane-ids.tsv'" in wezterm.LUA_CONFIG
        assert ".Kiln/" not in wezterm.LUA_CONFIG

    def test_reads_status_from_the_json_file_not_the_pane_title(self):
        # The agent rewrites its own OSC-0 title constantly and would win the race.
        assert "/.kiln/status/" in wezterm.LUA_CONFIG

    def test_defines_colours_for_scheduler_states(self):
        # The scheduler reports states the old wrapper never did.
        for state in ("retrying", "blocked", "idle"):
            assert f"{state} " in wezterm.LUA_CONFIG or f"{state}=" in wezterm.LUA_CONFIG

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


class TestTmux:
    def test_session_name_is_role_scoped(self):
        assert tmux.session_name("coder") == "kiln-coder"

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


class TestDispatch:
    @pytest.mark.parametrize("backend", [WEZTERM, WINDOWS_TERMINAL, TMUX])
    def test_dry_run_reaches_each_backend(self, backend):
        assert launch(backend, PANES, {}, Path("C:/p"), dry_run=True)

    def test_none_backend_only_logs(self):
        assert launch(NONE, PANES, {}, Path("C:/p")) == []

    def test_unknown_backend_raises(self):
        with pytest.raises(TerminalError, match="unknown terminal backend"):
            launch("kitty", PANES, {}, Path("C:/p"), dry_run=True)
