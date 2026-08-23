"""
WezTerm backend.

WezTerm builds the panes itself: this module writes a generated Lua config plus role/layout
JSON into the environment, then runs `wezterm start`. WezTerm's embedded Lua interpreter
handles `gui-startup` and does the actual `spawn_window`/`split`/`send_text` work.

The Lua below began as a verbatim port of the since-deleted lib/terminal-adapters/wezterm.ps1
template (in git history) with two bug fixes, both noted at their site: `KILN_PROJECT_DIR` is
now actually exported (it never was, which silently disabled the live status bar), and the
pane-id path casing is corrected. It has since gained keybindings and scheduler state colours.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import PaneSpec

log = logging.getLogger(__name__)

ENV_ROLES = "KILN_ROLES_JSON"
ENV_LAYOUT = "KILN_LAYOUT_JSON"
ENV_PROJECT_DIR = "KILN_PROJECT_DIR"
ENV_STATE_COLORS = "KILN_STATE_COLORS_JSON"

#: How long to leave the generated config in place before restoring the user's own, so
#: WezTerm has finished reading it.
CONFIG_SETTLE_SECONDS = 2

LUA_CONFIG = r"""
local wezterm = require 'wezterm'
local config = wezterm.config_builder()

-- Agent commands are generated as PowerShell syntax (e.g. `$env:VAR = '...'; ...`);
-- without this, panes fall back to the OS default shell (cmd.exe on some Windows
-- setups), which cannot parse that syntax and fails with a cryptic path/filename error.
if wezterm.target_triple:find('windows') then
  config.default_prog = { 'pwsh.exe', '-NoLogo' }
end

config.color_scheme        = "GitHub Dark"
config.enable_tab_bar      = true
config.window_decorations  = "TITLE | RESIZE"
config.initial_cols        = 120
config.initial_rows        = 40
config.font_size           = 9.0

config.mouse_bindings = {
  {
    event  = { Up = { streak = 1, button = 'Right' } },
    mods   = 'NONE',
    action = wezterm.action_callback(function(window, pane)
      window:perform_action(wezterm.action.PasteFrom 'Clipboard', pane)
    end),
  },
}

-- Familiar Windows copy/paste. Ctrl+C only copies when there is a selection; with none it
-- falls through to a normal interrupt, so stopping a running agent still works. Without
-- this, selecting scheduler output and pressing Ctrl+C kills the scheduler instead of
-- copying it.
config.keys = {
  {
    key = 'c',
    mods = 'CTRL',
    action = wezterm.action_callback(function(window, pane)
      local selection = window:get_selection_text_for_pane(pane)
      if selection and #selection > 0 then
        window:perform_action(wezterm.action.CopyTo 'ClipboardAndPrimarySelection', pane)
        window:perform_action(wezterm.action.ClearSelection, pane)
      else
        window:perform_action(wezterm.action.SendKey { key = 'c', mods = 'CTRL' }, pane)
      end
    end),
  },
  {
    key = 'v',
    mods = 'CTRL',
    action = wezterm.action.PasteFrom 'Clipboard',
  },
}

local role_map    = {}
local roles_json  = os.getenv('KILN_ROLES_JSON') or '[]'
local roles       = wezterm.json_parse(roles_json)
local project_dir = os.getenv('KILN_PROJECT_DIR') or ''

-- Read, not hardcoded: `scheduler.pane_status.STATE_COLORS_HEX` (Python) is the single
-- source of truth for state colour, exported here as JSON by `build_environment` so this
-- badge and the pane's own bottom-row status bar can no longer drift into two different
-- palettes for the same state names -- which is exactly what a hand-copied second table
-- here used to let happen.
local state_colors_json = os.getenv('KILN_STATE_COLORS_JSON') or '{}'
local STATE_COLORS = wezterm.json_parse(state_colors_json) or {}
local STATE_COLOR_DEFAULT = '#8a8a88'

wezterm.on('format-window-title', function(tab, pane, tabs, panes, config)
  local title = tab.tab_title
  if title and #title > 0 then
    return 'Kiln - ' .. title
  end
  return 'Kiln'
end)

-- Reads .kiln/status/<role>.json rather than the pane's OSC-0 title: the agent running in
-- that pane rewrites its own title on every render tick and would always win the race.
wezterm.on('update-status', function(window, pane)
  if not roles or #roles == 0 or project_dir == '' then
    return
  end

  -- Rendered by WezTerm itself, not through Python's stdout, so no encoding concern here.
  local MODE_EMOJIS = { auto = '🤖', manual = '🧑' }
  local segments = {}

  -- Stateless panes (inbox, dashboard, cockpit) never write a status file, and the
  -- `mode == 'manual'` fallback below would badge them 'waiting' -- which they never are.
  -- Filtered into their own list first rather than skipped inside the loop, because the
  -- separator test is `i < #roles`: skipping the last entry mid-loop would leave a
  -- trailing separator with nothing after it.
  local shown = {}
  for _, r in ipairs(roles) do
    if not r.passive then
      table.insert(shown, r)
    end
  end

  for i, r in ipairs(shown) do
    local title = nil
    local state = nil
    local mode = r.mode or 'auto'
    local status_path = project_dir .. '/.kiln/status/' .. r.role .. '.json'
    local f = io.open(status_path, 'r')
    if f then
      local content = f:read('*a')
      f:close()
      local ok, status = pcall(wezterm.json_parse, content)
      if ok and status then
        title = status.title
        state = status.state
        mode = status.mode or mode
      end
    end
    if not title or #title == 0 then
      title = r.name or r.role
    end

    if mode == 'manual' and (not state or #state == 0) then
      state = 'waiting'
    end

    local display_title = (MODE_EMOJIS[mode] or '') .. ' ' .. title
    table.insert(segments, { Background = { Color = STATE_COLORS[state] or STATE_COLOR_DEFAULT } })
    table.insert(segments, { Foreground = { Color = '#000000' } })
    table.insert(segments, { Text = ' ' .. display_title .. ' ' })
    table.insert(segments, 'ResetAttributes')
    if i < #shown then
      table.insert(segments, { Text = ' ' })
    end
  end
  table.insert(segments, { Text = '  ' })

  window:set_right_status(wezterm.format(segments))
end)

local function find_role(role_name)
  for _, r in ipairs(roles) do
    if r.role == role_name then
      return r
    end
  end
  return nil
end

wezterm.on('gui-startup', function(cmd)
  local mux = wezterm.mux
  local layout_json = os.getenv('KILN_LAYOUT_JSON') or ''

  if not roles or #roles == 0 then
    return
  end

  local layout = nil
  if layout_json and layout_json ~= '' then
    layout = wezterm.json_parse(layout_json)
  end

  local all_panes = {}

  if layout and layout.tabs then
    local window = nil
    local first_tab = true

    for _, tab_def in ipairs(layout.tabs) do
      if tab_def.panes and #tab_def.panes > 0 then
        local first_role = find_role(tab_def.panes[1].role)
        if first_role then
          local tab, first_pane
          if first_tab then
            tab, first_pane, window = mux.spawn_window({ cwd = first_role.path })
            first_tab = false
          else
            tab, first_pane = window:spawn_tab({ cwd = first_role.path })
          end

          if tab_def.title then
            tab:set_title(tab_def.title)
          else
            local role_names = {}
            for _, pane_def in ipairs(tab_def.panes) do
              local r = find_role(pane_def.role)
              if r then table.insert(role_names, r.name) end
            end
            if #role_names > 0 then
              tab:set_title(table.concat(role_names, ' & '))
            end
          end

          first_pane:send_text(first_role.cmd .. '\r\n')
          table.insert(all_panes, first_pane)
          role_map[first_role.role] = first_pane:pane_id()

          -- Take the grid path only when the tab actually ASKED for a grid. The old test
          -- defaulted grid_cols to #panes, so any tab with two panes fell into the grid
          -- branch and had its split direction hardcoded to 'Right' -- which silently
          -- overrode the per-pane 'direction' honoured in the simple branch below.
          local grid_rows = tab_def.gridRows or 1
          local grid_cols = tab_def.gridCols or #tab_def.panes

          if tab_def.gridRows or tab_def.gridCols then
            local panes_grid = { [1] = { [1] = first_pane } }
            local prev_row_pane = first_pane

            for row = 2, grid_rows do
              local idx = (row - 1) * grid_cols + 1
              if idx <= #tab_def.panes then
                local role_data = find_role(tab_def.panes[idx].role)
                if role_data then
                  local new_pane = prev_row_pane:split({
                    direction = 'Bottom',
                    size = 1.0 / (grid_rows - row + 2),
                    cwd = role_data.path,
                  })
                  panes_grid[row] = panes_grid[row] or {}
                  panes_grid[row][1] = new_pane
                  new_pane:send_text(role_data.cmd .. '\r\n')
                  table.insert(all_panes, new_pane)
                  role_map[role_data.role] = new_pane:pane_id()
                  prev_row_pane = new_pane
                end
              end
            end

            for col = 2, grid_cols do
              for row = 1, grid_rows do
                local idx = (row - 1) * grid_cols + col
                if idx <= #tab_def.panes then
                  local role_data = find_role(tab_def.panes[idx].role)
                  if role_data and panes_grid[row] and panes_grid[row][col - 1] then
                    local new_pane = panes_grid[row][col - 1]:split({
                      direction = 'Right',
                      size = 1.0 / (grid_cols - col + 2),
                      cwd = role_data.path,
                    })
                    panes_grid[row][col] = new_pane
                    new_pane:send_text(role_data.cmd .. '\r\n')
                    table.insert(all_panes, new_pane)
                    role_map[role_data.role] = new_pane:pane_id()
                  end
                end
              end
            end
          else
            local prev_pane = first_pane
            for idx = 2, #tab_def.panes do
              local pane_def = tab_def.panes[idx]
              local role_data = find_role(pane_def.role)
              if role_data then
                -- 'direction' and 'size' are optional per pane. The defaults reproduce the
                -- old behaviour (split rightwards into equal shares); an inbox sets
                -- direction='Bottom' with a small size so it is a strip, not a column.
                local new_pane = prev_pane:split({
                  direction = pane_def.direction or 'Right',
                  size = pane_def.size or (1.0 / (#tab_def.panes - idx + 2)),
                  cwd = role_data.path,
                })
                new_pane:send_text(role_data.cmd .. '\r\n')
                table.insert(all_panes, new_pane)
                role_map[role_data.role] = new_pane:pane_id()
                prev_pane = new_pane
              end
            end
          end
        end
      end
    end

    if window then
      window:tabs()[1]:activate()
    end

  else
    -- No layout: one tab per role.
    local window = nil
    for i, r in ipairs(roles) do
      local tab, pane
      if i == 1 then
        tab, pane, window = mux.spawn_window({ cwd = r.path })
        tab:activate()
      else
        tab, pane = window:spawn_tab({ cwd = r.path })
      end
      tab:set_title(r.name)
      pane:send_text(r.cmd .. '\r\n')
      table.insert(all_panes, pane)
      role_map[r.role] = pane:pane_id()
    end
  end

  -- Pane ids for tooling that addresses agents via `wezterm cli`.
  -- NOTE: lowercase '.kiln' — the PowerShell original wrote '.Kiln', which happened to
  -- work on case-insensitive Windows but created a stray directory elsewhere.
  if project_dir ~= '' then
    local f = io.open(project_dir .. '/.kiln/pane-ids.tsv', 'w')
    if f then
      for role_name, pane_id in pairs(role_map) do
        f:write(role_name .. '\t' .. pane_id .. '\n')
      end
      f:close()
    end
  end
end)

return config
"""


def build_roles_json(panes: list[PaneSpec]) -> str:
    return json.dumps([pane.as_role_data() for pane in panes], separators=(",", ":"))


def config_path() -> Path:
    return Path.home() / ".wezterm.lua"


def build_environment(panes: list[PaneSpec], layout: dict, project_dir: Path) -> dict[str, str]:
    """
    Environment handed to WezTerm.

    `KILN_PROJECT_DIR` is essential and was missing from the PowerShell original: the Lua
    `update-status` handler returns immediately when it is empty, which silently disabled
    the live status bar the README advertises.
    """
    # Deferred: only importable once `prepare()` has put `src` on sys.path (see
    # cli.py), and this module must stay importable before that has happened.
    from kiln.scheduler.infrastructure.terminal.pane_status import STATE_COLORS_HEX

    env = {
        ENV_ROLES: build_roles_json(panes),
        # Lua does `project_dir .. '/.kiln/...'`, so forward slashes keep the path valid.
        ENV_PROJECT_DIR: project_dir.as_posix(),
        ENV_STATE_COLORS: json.dumps(STATE_COLORS_HEX, separators=(",", ":")),
    }
    if layout:
        env[ENV_LAYOUT] = json.dumps(layout, separators=(",", ":"))
    return env


def launch(
    panes: list[PaneSpec], layout: dict, project_dir: Path, dry_run: bool = False
) -> list[str]:
    """
    Write the generated config, start WezTerm, then restore the user's own config.

    The user's `~/.wezterm.lua` is backed up and restored rather than merged: WezTerm only
    reads one config file, and leaving Kiln's in place would change every later session.
    """
    command = ["wezterm", "start"]
    if dry_run:
        return command

    if not shutil.which("wezterm"):
        from . import TerminalError

        raise TerminalError("wezterm not found on PATH")

    target = config_path()
    backup = target.with_suffix(".lua.kiln-backup")
    had_existing = target.exists()
    if had_existing:
        shutil.copy2(target, backup)

    environment = {**os.environ, **build_environment(panes, layout, project_dir)}
    try:
        target.write_text(LUA_CONFIG, encoding="utf-8")
        subprocess.run(command, env=environment, check=False)
    finally:
        # WezTerm reads the config asynchronously after start returns.
        time.sleep(CONFIG_SETTLE_SECONDS)
        if had_existing:
            shutil.move(str(backup), str(target))
        elif target.exists():
            target.unlink()

    return command
