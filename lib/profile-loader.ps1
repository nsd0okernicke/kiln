#!/usr/bin/env pwsh
# Profile Loader Module for Kiln
# Loads YAML profiles and extracts terminal/agent configuration

<#
.SYNOPSIS
Load a Kiln configuration profile into environment variables.

.DESCRIPTION
Reads kiln/profiles.json and sets TERMINAL_* environment variables for the selected profile.
Supports cascading lookup: project-local > user config > system config.

.PARAMETER ProjectRoot
Path to the project root where profiles.json is located.

.PARAMETER Profile
Name of the profile to load (default: 'dev').

.PARAMETER ConfigPath
Optional path to profiles.json. If not provided, searches standard locations.
#>

function Load-KilnProfile {
    param(
        [Parameter(Mandatory=$true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory=$false)]
        [string]$Profile = "dev",

        [Parameter(Mandatory=$false)]
        [string]$ConfigPath = "",

        [Parameter(Mandatory=$false)]
        [string]$FrameworkRoot = ""
    )

    # Find profiles.json if not provided
    if (-not $ConfigPath) {
        $searchPaths = @(
            (Join-Path $ProjectRoot "kiln.profiles.json"),
            (Join-Path $ProjectRoot "kiln" "profiles.json"),
            (Join-Path $ProjectRoot ".kiln" "profiles.json")
        )

        # Add framework root if provided
        if ($FrameworkRoot) {
            $searchPaths += (Join-Path $FrameworkRoot "kiln" "profiles.json")
        }

        # Add user and system paths
        $searchPaths += @(
            (Join-Path $env:USERPROFILE ".kiln" "profiles.json"),
            "C:\ProgramData\kiln\profiles.json"
        )

        foreach ($path in $searchPaths) {
            if (Test-Path $path) {
                $ConfigPath = $path
                Write-Verbose "Found profiles.json at: $ConfigPath"
                break
            }
        }

        if (-not $ConfigPath) {
            throw "Could not find profiles.json. Searched: $($searchPaths -join ', ')"
        }
    }

    # Load and parse JSON
    Write-Verbose "Loading profile '$Profile' from: $ConfigPath"
    $jsonContent = Get-Content -Path $ConfigPath -Raw
    $config = $jsonContent | ConvertFrom-Json

    # Find the selected profile
    if (-not $config.profiles.PSObject.Properties.Name -contains $Profile) {
        $availableProfiles = $config.profiles.PSObject.Properties.Name -join ", "
        throw "Profile '$Profile' not found. Available profiles: $availableProfiles"
    }

    $selectedProfile = $config.profiles.$Profile
    Write-Verbose "Loaded profile: $Profile"
    Write-Verbose "Description: $($selectedProfile.description)"

    # Output variable assignments that can be evaluated in the caller's scope
    $assignments = @()
    $desc = $selectedProfile.description -replace "'", "''"
    $assignments += "`$PROFILE_NAME = '$Profile';"
    $assignments += "`$PROFILE_DESCRIPTION = '$desc';"
    $assignments += "`$TERMINAL_COUNT = $($selectedProfile.terminals.Count);"

    # Serialize layout as JSON
    if ($selectedProfile.layout) {
        $layoutJson = $selectedProfile.layout | ConvertTo-Json -Depth 10 -Compress
        # Escape single quotes for PowerShell string
        $layoutJson = $layoutJson -replace "'", "''"
        $assignments += "`$PROFILE_LAYOUT_JSON = '$layoutJson';"
    }

    for ($i = 0; $i -lt $selectedProfile.terminals.Count; $i++) {
        $terminal = $selectedProfile.terminals[$i]
        $role = $terminal.role -replace "'", "''"
        $worktree = $terminal.worktree -replace "'", "''"
        $agent = $terminal.agent -replace "'", "''"
        $model = $terminal.model -replace "'", "''"
        $assignments += "`$TERMINAL_${i}_ROLE = '$role';"
        $assignments += "`$TERMINAL_${i}_WORKTREE = '$worktree';"
        $assignments += "`$TERMINAL_${i}_AGENT = '$agent';"
        if ($model) {
            $assignments += "`$TERMINAL_${i}_MODEL = '$model';"
        }
        $mode = ""
        if ($terminal.PSObject.Properties.Name -contains "mode") {
            $mode = $terminal.mode -replace "'", "''"
        }
        if ($mode) {
            $assignments += "`$TERMINAL_${i}_MODE = '$mode';"
        }
    }

    return $assignments -join " "
}


