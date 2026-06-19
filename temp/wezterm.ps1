#!/usr/bin/env pwsh
# WezTerm adapter for SwarmForge (Windows/PowerShell)
# Uses dynamic Lua config generated at runtime to build multi-pane layouts
# Requires: wezterm in PATH

function Get-TerminalBackendLabel {
    return "WezTerm"
}

function Test-TerminalCanOpenSessions {
    return $true
}

function Test-TerminalTracksWindows {
    return $false
}

function Test-TerminalWindowExists {
    param([string]$PaneId)
    return $false
}

function Invoke-TerminalOpenSession {
    # Deprecated — use Start-WezTermSession instead
    param(
        [string]$Session,
        [string]$Title,
        [string]$WorkingDir,
        [string]$Agent = "claude",
        [string]$SiblingPaneId = ""
    )
    Write-Host "  Warning: Invoke-TerminalOpenSession is deprecated for WezTerm; use Start-WezTermSession" -ForegroundColor Yellow
    return ""
}

function Invoke-TerminalCloseWindow {
    param([string]$PaneId)
    # Window tracking not supported
}

function Build-AgentCommand {
    param(
        [string]$Agent,
        [string]$DisplayName,
        [string]$WorktreePath,
        [string]$Role
    )

    switch ($Agent) {
        "claude" {
            # CRITICAL: Set SWARMFORGE_ROLE so check-inbox can find messages for this agent
            return "`$env:SWARMFORGE_ROLE='$Role'; claude --model claude-haiku-4-5-20251001 --permission-mode bypassPermissions -n '$DisplayName'"
        }
        "copilot" {
            return "`$env:SWARMFORGE_ROLE='$Role'; copilot -C '$WorktreePath' --name '$DisplayName'"
        }
        default {
            return "echo 'Agent $Agent not supported'"
        }
    }
}

function Start-WezTermSession {
    param(
        [PSCustomObject[]]$RoleData,
        [string]$Layout = "panes",
        [hashtable]$ProfileLayout = $null
    )

    # Build JSON for all roles (passed to Lua via env var)
    $rolesJson = $RoleData | ConvertTo-Json -Depth 5 -Compress
    $env:SWARMFORGE_ROLES_JSON = $rolesJson
    $env:SWARMFORGE_LAYOUT = $Layout

    # Pass profile layout if provided (takes precedence over simple Layout parameter)
    if ($ProfileLayout) {
        $layoutJson = $ProfileLayout | ConvertTo-Json -Depth 10 -Compress
        $env:SWARMFORGE_PROFILE_LAYOUT = $layoutJson
    }

    # WezTerm loads config from ~/.wezterm.lua by default
    # We write our generated config there, with backup/restore
    $wezConfigPath = Join-Path $env:USERPROFILE ".wezterm.lua"
    $backupPath = "$wezConfigPath.swarmforge-backup"

    # Backup existing config if present
    if (Test-Path $wezConfigPath) {
        Copy-Item -Path $wezConfigPath -Destination $backupPath -Force
    }

    try {
        # Write generated Lua config
        $luaContent = Get-LuaConfigTemplate
        Set-Content -Path $wezConfigPath -Value $luaContent -Encoding UTF8

        # Launch WezTerm (it will auto-load ~/.wezterm.lua)
        & wezterm.exe start

    } catch {
        Write-Host "Error: Failed to start WezTerm: $_" -ForegroundColor Red
    } finally {
        # Restore original config after WezTerm starts
        Start-Sleep -Milliseconds 500
        if (Test-Path $backupPath) {
            Move-Item -Path $backupPath -Destination $wezConfigPath -Force
        } elseif (Test-Path $wezConfigPath) {
            Remove-Item -Path $wezConfigPath -Force
        }
    }
}

function Get-LuaConfigTemplate {
    @'
local wezterm = require 'wezterm'
local config = wezterm.config_builder()

-- Layout and appearance
config.color_scheme        = "GitHub Dark"
config.enable_tab_bar      = true
config.window_decorations  = "TITLE | RESIZE"
config.initial_cols        = 120
config.initial_rows        = 40
config.font_size           = 9.0

-- Right-click to paste from clipboard
config.mouse_bindings = {
  {
    event  = { Up = { streak = 1, button = 'Right' } },
    mods   = 'NONE',
    action = wezterm.action_callback(function(window, pane)
      window:perform_action(wezterm.action.PasteFrom 'Clipboard', pane)
    end),
  },
}

wezterm.on('gui-startup', function(cmd)
  local mux = wezterm.mux
  local roles_json       = os.getenv('SWARMFORGE_ROLES_JSON')   or '[]'
  local layout           = os.getenv('SWARMFORGE_LAYOUT')       or 'panes'
  local project_dir      = os.getenv('SWARMFORGE_PROJECT_DIR')  or ''
  local profile_layout   = os.getenv('SWARMFORGE_PROFILE_LAYOUT') or ''
  local roles = wezterm.json_parse(roles_json)

  if not roles or #roles == 0 then
    return
  end

  -- Index roles by name for quick lookup
  local role_index = {}
  for i, role in ipairs(roles) do
    role_index[role.role] = role
  end

  -- Cycle color scheme based on agent count
  local color_schemes = {
    "GitHub Dark",
    "Dracula",
    "Solarized (dark) (terminal.sexy)",
    "Gruvbox Dark",
    "Nord",
    "One Half Dark",
    "Catppuccin Mocha",
    "Tokyo Night",
  }
  local scheme_idx = (#roles % #color_schemes) + 1
  config.color_scheme = color_schemes[scheme_idx]

  local all_panes = {}

  -- Use profile layout if available
  if profile_layout and profile_layout ~= '' then
    local profile = wezterm.json_parse(profile_layout)
    if profile and profile.tabs then
      -- Create tabs and panes based on profile layout
      local window = nil
      local first_tab = true
      local tab_idx = 0

      for _, tab_def in ipairs(profile.tabs) do
        tab_idx = tab_idx + 1
        local tab = nil
        local panes_in_tab = {}

        if first_tab then
          -- First tab: spawn window with first pane (explicitly use PowerShell on Windows)
          if tab_def.panes and #tab_def.panes > 0 then
            local first_role = role_index[tab_def.panes[1].role]
            if first_role then
              tab, panes_in_tab[1], window = mux.spawn_window({
                cwd = first_role.path,
                args = { 'pwsh.exe', '-NoExit' }
              })
              tab:set_title(tab_def.title or ('Tab ' .. tab_idx))
              panes_in_tab[1]:send_text(first_role.cmd .. '\r\n')
              first_tab = false
            end
          end
        else
          -- Subsequent tabs: spawn from window (explicitly use PowerShell on Windows)
          if tab_def.panes and #tab_def.panes > 0 then
            local first_role = role_index[tab_def.panes[1].role]
            if first_role then
              tab, panes_in_tab[1] = window:spawn_tab({
                cwd = first_role.path,
                args = { 'pwsh.exe', '-NoExit' }
              })
              tab:set_title(tab_def.title or ('Tab ' .. tab_idx))
              panes_in_tab[1]:send_text(first_role.cmd .. '\r\n')
            end
          end
        end

        -- Add remaining panes in this tab (split vertically)
        if tab_def.panes then
          for pane_idx = 2, #tab_def.panes do
            local role_def = tab_def.panes[pane_idx]
            local role_data = role_index[role_def.role]
            if role_data and panes_in_tab[pane_idx - 1] then
              local split_size = 1.0 / (#tab_def.panes - pane_idx + 2)
              panes_in_tab[pane_idx] = panes_in_tab[pane_idx - 1]:split({
                direction = 'Right',
                size = split_size,
                cwd = role_data.path
              })
              -- Split panes default to cmd.exe; explicitly invoke PowerShell
              panes_in_tab[pane_idx]:send_text('pwsh -NoExit -Command "' .. role_data.cmd .. '"\r\n')
            end
          end
        end

        -- Collect all panes
        for _, pane in ipairs(panes_in_tab) do
          all_panes[#all_panes + 1] = pane
        end
      end

      if window then
        window:tabs()[1]:activate()
      end
    end

  elseif layout == 'tabs' then
    local markers = { '🟦', '🟥', '🟩', '🟨', '🟪', '🟫', '⬜', '⬛' }
    local tab, first_pane, window = mux.spawn_window({
      cwd = roles[1].path,
      args = { 'pwsh.exe', '-NoExit' }
    })
    local marker_idx = ((0) % #markers) + 1
    tab:set_title(markers[marker_idx] .. ' ' .. roles[1].name)
    first_pane:send_text(roles[1].cmd .. '\r\n')
    all_panes[1] = first_pane

    for i = 2, #roles do
      local tab_n, pane = window:spawn_tab({
        cwd = roles[i].path,
        args = { 'pwsh.exe', '-NoExit' }
      })
      marker_idx = ((i-1) % #markers) + 1
      tab_n:set_title(markers[marker_idx] .. ' ' .. roles[i].name)
      pane:send_text(roles[i].cmd .. '\r\n')
      all_panes[i] = pane
    end
    tab:activate()

  else
    -- Panes layout: 2 columns, dynamic rows, left column fills first
    local n = #roles
    local left_count  = math.ceil(n / 2)
    local right_count = math.floor(n / 2)

    -- Distribute roles: left col gets odd positions (1,3,5...), right gets even (2,4,6...)
    local left_roles  = {}
    local right_roles = {}
    for i = 1, n do
      if i % 2 == 1 then
        left_roles[#left_roles + 1] = roles[i]
      else
        right_roles[#right_roles + 1] = roles[i]
      end
    end

    -- Spawn window with top-left pane (explicitly use PowerShell on Windows)
    local tab, left_top, window = mux.spawn_window({
      cwd = left_roles[1].path,
      args = { 'pwsh.exe', '-NoExit' }
    })
    tab:set_title('SwarmForge v0.1')

    -- Split right column from left_top (top-right pane)
    local right_top = nil
    if right_count > 0 then
      right_top = left_top:split({
        direction = 'Right',
        size = 0.5,
        cwd = right_roles[1].path
      })
    end

    -- Build left column: split Bottom from each previous left pane
    local left_panes = { left_top }
    for i = 2, left_count do
      local size = 1.0 / (left_count - i + 2)
      left_panes[i] = left_panes[i-1]:split({
        direction = 'Bottom',
        size = size,
        cwd = left_roles[i].path
      })
    end

    -- Build right column: split Bottom from each previous right pane
    local right_panes = {}
    if right_count > 0 then
      right_panes[1] = right_top
      for i = 2, right_count do
        local size = 1.0 / (right_count - i + 2)
        right_panes[i] = right_panes[i-1]:split({
          direction = 'Bottom',
          size = size,
          cwd = right_roles[i].path
        })
      end
    end

    -- Send commands and collect all_panes in role order
    for i = 1, left_count do
      local cmd = left_roles[i].cmd
      cmd = cmd:gsub(" -n '[^']*'", "")
      cmd = cmd:gsub(' -n "[^"]*"', "")
      cmd = cmd:gsub(" --name '[^']*'", "")
      cmd = cmd:gsub(' --name "[^"]*"', "")
      -- Left panes are split and default to cmd.exe; explicitly invoke PowerShell
      left_panes[i]:send_text('pwsh -NoExit -Command "' .. cmd .. '"\r\n')
      all_panes[#all_panes + 1] = left_panes[i]
      if right_panes[i] then
        local rcmd = right_roles[i].cmd
        rcmd = rcmd:gsub(" -n '[^']*'", "")
        rcmd = rcmd:gsub(' -n "[^"]*"', "")
        rcmd = rcmd:gsub(" --name '[^']*'", "")
        rcmd = rcmd:gsub(' --name "[^"]*"', "")
        -- Right panes are split and default to cmd.exe; explicitly invoke PowerShell
        right_panes[i]:send_text('pwsh -NoExit -Command "' .. rcmd .. '"\r\n')
        all_panes[#all_panes + 1] = right_panes[i]
      end
    end

    tab:activate()
  end

  -- Write pane IDs so the watchdog can address agents directly via wezterm cli
  if project_dir ~= '' then
    local pane_ids_path = project_dir .. '/.swarmforge/pane-ids.tsv'
    local f = io.open(pane_ids_path, 'w')
    if f then
      for i, pane in ipairs(all_panes) do
        if roles[i] and roles[i].role then
          f:write(roles[i].role .. '\t' .. pane:pane_id() .. '\n')
        end
      end
      f:close()
    end
  end
end)

return config
'@
}
