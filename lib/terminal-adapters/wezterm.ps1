#!/usr/bin/env pwsh
# WezTerm adapter for Kiln (Windows/PowerShell)
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
        [string]$WorktreePath
    )

    switch ($Agent) {
        "claude" {
            return "claude --model claude-haiku-4-5-20251001 --permission-mode bypassPermissions --mcp-config ./.mcp.json -n '$DisplayName'"
        }
        "copilot" {
            # Copilot auto-detects mcp-config.json from ~/.copilot/
            return "copilot --allow-all --name '$DisplayName'"
        }
        default {
            return "echo 'Agent $Agent not supported'"
        }
    }
}

function Start-WezTermSession {
    param(
        [PSCustomObject[]]$RoleData,
        [string]$LayoutJson = ""
    )

    # Build JSON for all roles (passed to Lua via env var)
    $rolesJson = $RoleData | ConvertTo-Json -Depth 5 -Compress
    Write-Verbose "Roles JSON: $rolesJson"
    $env:Kiln_ROLES_JSON = $rolesJson

    # Pass layout structure as JSON (may be empty, Lua will handle it)
    if (-not [string]::IsNullOrEmpty($LayoutJson)) {
        $env:Kiln_LAYOUT_JSON = $LayoutJson
    }

    # Backup user's config, use ours temporarily, then restore
    # This avoids long-term interference with the user's .wezterm.lua
    $wezConfigPath = Join-Path $env:USERPROFILE ".wezterm.lua"
    $backupPath = "$wezConfigPath.Kiln-backup"

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
        # Wait for WezTerm to fully load config before restoring
        Start-Sleep -Seconds 2

        # Restore original config
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
  local roles_json      = os.getenv('Kiln_ROLES_JSON')      or '[]'
  local layout_json     = os.getenv('Kiln_LAYOUT_JSON')     or ''
  local project_dir     = os.getenv('Kiln_PROJECT_DIR')     or ''
  local roles = wezterm.json_parse(roles_json)

  if not roles or #roles == 0 then
    return
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
  local role_map = {}

  -- Parse layout structure
  local layout = nil
  if layout_json and layout_json ~= '' then
    layout = wezterm.json_parse(layout_json)
  end

  -- Build layout from profile structure
  if layout and layout.tabs then
    -- New flat format: layout.tabs is array of { title, panes: [] }
    local window = nil
    local first_tab = true

    for tab_idx, tab_def in ipairs(layout.tabs) do
      if not tab_def.panes or #tab_def.panes == 0 then
        goto next_tab
      end

      local first_role_name = tab_def.panes[1].role
      local first_role = nil
      for _, r in ipairs(roles) do
        if r.role == first_role_name then
          first_role = r
          break
        end
      end

      if not first_role then
        goto next_tab
      end

      -- Create first pane
      local tab, first_pane
      if first_tab then
        tab, first_pane, window = mux.spawn_window({ cwd = first_role.path })
        first_tab = false
      else
        tab, first_pane = window:spawn_tab({ cwd = first_role.path })
      end

      -- Set tab title: explicit title or derive from role names
      if tab_def.title then
        tab:set_title(tab_def.title)
      else
        -- Generate title from all role names in this tab: "Role1 & Role2 & Role3"
        local role_names = {}
        for _, pane_def in ipairs(tab_def.panes) do
          for _, r in ipairs(roles) do
            if r.role == pane_def.role then
              table.insert(role_names, r.name)
              break
            end
          end
        end
        if #role_names > 0 then
          tab:set_title(table.concat(role_names, ' & '))
        end
      end

      -- Send command to first pane
      first_pane:send_text(first_role.cmd .. '\r\n')
      table.insert(all_panes, first_pane)
      role_map[first_role_name] = first_pane:pane_id()

      -- Add remaining panes via splits
      local grid_rows = tab_def.gridRows or 1
      local grid_cols = tab_def.gridCols or #tab_def.panes

      if grid_rows > 1 or grid_cols > 1 then
        -- Grid layout: create a 2D grid of panes
        local panes_grid = {}
        panes_grid[1] = {}
        panes_grid[1][1] = first_pane

        -- Create first column (all rows in column 1)
        local prev_row_pane = first_pane
        for row = 2, grid_rows do
          local col1_pane_idx = (row - 1) * grid_cols + 1
          if col1_pane_idx <= #tab_def.panes then
            local pane_def = tab_def.panes[col1_pane_idx]
            local role_data = nil
            for _, r in ipairs(roles) do
              if r.role == pane_def.role then
                role_data = r
                break
              end
            end
            if role_data then
              local split_size = 1.0 / (grid_rows - row + 2)
              local new_pane = prev_row_pane:split({
                direction = 'Bottom',
                size = split_size,
                cwd = role_data.path
              })
              if not panes_grid[row] then panes_grid[row] = {} end
              panes_grid[row][1] = new_pane
              new_pane:send_text(role_data.cmd .. '\r\n')
              table.insert(all_panes, new_pane)
              role_map[pane_def.role] = new_pane:pane_id()
              prev_row_pane = new_pane
            end
          end
        end

        -- Create remaining columns
        for col = 2, grid_cols do
          for row = 1, grid_rows do
            local pane_idx = (row - 1) * grid_cols + col
            if pane_idx <= #tab_def.panes then
              local pane_def = tab_def.panes[pane_idx]
              local role_data = nil
              for _, r in ipairs(roles) do
                if r.role == pane_def.role then
                  role_data = r
                  break
                end
              end
              if role_data and panes_grid[row] and panes_grid[row][col - 1] then
                local split_size = 1.0 / (grid_cols - col + 2)
                local new_pane = panes_grid[row][col - 1]:split({
                  direction = 'Right',
                  size = split_size,
                  cwd = role_data.path
                })
                if not panes_grid[row] then panes_grid[row] = {} end
                panes_grid[row][col] = new_pane
                new_pane:send_text(role_data.cmd .. '\r\n')
                table.insert(all_panes, new_pane)
                role_map[pane_def.role] = new_pane:pane_id()
              end
            end
          end
        end
      else
        -- Simple linear layout: split horizontally for each pane
        local prev_pane = first_pane
        for pane_idx = 2, #tab_def.panes do
          local pane_def = tab_def.panes[pane_idx]
          local role_name = pane_def.role
          local role_data = nil
          for _, r in ipairs(roles) do
            if r.role == role_name then
              role_data = r
              break
            end
          end

          if role_data then
            local split_size = 1.0 / (#tab_def.panes - pane_idx + 2)
            local new_pane = prev_pane:split({
              direction = 'Right',
              size = split_size,
              cwd = role_data.path
            })

            new_pane:send_text(role_data.cmd .. '\r\n')
            table.insert(all_panes, new_pane)
            role_map[role_name] = new_pane:pane_id()
            prev_pane = new_pane
          end
        end
      end

      ::next_tab::
    end

    if window then
      window:tabs()[1]:activate()
    end

  elseif layout and layout.roles then
    -- Fallback: old shorthand format with roles array
    local markers = { '🟦', '🟥', '🟩', '🟨', '🟪', '🟫', '⬜', '⬛' }
    local window = nil
    for i, role_name in ipairs(layout.roles) do
      local role_obj = nil
      for _, r in ipairs(roles) do
        if r.role == role_name then
          role_obj = r
          break
        end
      end
      if role_obj then
        local tab, pane
        if i == 1 then
          tab, pane, window = mux.spawn_window({ cwd = role_obj.path })
          tab:activate()
        else
          tab, pane = window:spawn_tab({ cwd = role_obj.path })
        end
        local marker_idx = ((i - 1) % #markers) + 1
        tab:set_title(markers[marker_idx] .. ' ' .. role_obj.name)
        pane:send_text('cd "' .. role_obj.path .. '" && ' .. role_obj.cmd .. '\r\n')
        table.insert(all_panes, pane)
        role_map[role_obj.role] = pane:pane_id()
      end
    end
  end

  -- Write pane IDs so the watchdog can address agents directly via wezterm cli
  if project_dir ~= '' then
    local pane_ids_path = project_dir .. '/.Kiln/pane-ids.tsv'
    local f = io.open(pane_ids_path, 'w')
    if f then
      for role_name, pane_id in pairs(role_map) do
        f:write(role_name .. '\t' .. pane_id .. '\n')
      end
      f:close()
    end
  end
end)

return config
'@
}

