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
            assert set(role) >= {"role", "name", "path", "cmd", "mode", "passive"}

    def test_roles_json_marks_stateless_panes(self):
        # The Lua cannot work this out for itself -- it never sees a profile -- so the flag
        # has to ride along with the pane it describes.
        panes = [*PANES, PaneSpec(
            role="cockpit", name="Cockpit", path="C:/proj", cmd="python -m cockpit.server",
            mode="manual", passive=True,
        )]

        roles = json.loads(wezterm.build_environment(panes, {}, Path("C:/p"))[wezterm.ENV_ROLES])

        assert {r["role"]: r["passive"] for r in roles} == {
            "specifier": False, "coder": False, "cockpit": True,
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
        from scheduler.pane_status import STATE_COLORS_HEX

        env = wezterm.build_environment(PANES, {}, Path("C:/proj"))
        assert json.loads(env[wezterm.ENV_STATE_COLORS]) == STATE_COLORS_HEX


class TestWezTermLua:
    def test_reads_the_environment_variables_the_launcher_sets(self):
        for name in (
            wezterm.ENV_ROLES, wezterm.ENV_LAYOUT, wezterm.ENV_PROJECT_DIR,
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
        from scheduler.pane_status import STATE_COLORS_HEX

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
