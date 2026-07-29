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
Name of the profile to load (default: 'default').

.PARAMETER ConfigPath
Optional path to profiles.json. If not provided, searches standard locations.
#>

function Find-KilnProfilesConfig {
    param(
        [Parameter(Mandatory=$true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory=$false)]
        [string]$FrameworkRoot = ""
    )

    $searchPaths = @(
        (Join-Path -Path $ProjectRoot -ChildPath "kiln.profiles.json"),
        (Join-Path -Path $ProjectRoot -ChildPath (Join-Path -Path "kiln" -ChildPath "profiles.json")),
        (Join-Path -Path $ProjectRoot -ChildPath (Join-Path -Path ".kiln" -ChildPath "profiles.json"))
    )

    # Add framework root if provided
    if ($FrameworkRoot) {
        $searchPaths += (Join-Path -Path $FrameworkRoot -ChildPath (Join-Path -Path "kiln" -ChildPath (Join-Path -Path "framework" -ChildPath "profiles.json")))
    }

    # Add user and system paths
    $searchPaths += @(
        (Join-Path -Path $env:USERPROFILE -ChildPath (Join-Path -Path ".kiln" -ChildPath "profiles.json")),
        "C:\ProgramData\kiln\profiles.json"
    )

    foreach ($path in $searchPaths) {
        if (Test-Path $path) {
            Write-Verbose "Found profiles.json at: $path"
            return $path
        }
    }

    throw "Could not find profiles.json. Searched: $($searchPaths -join ', ')"
}

function Get-KilnDefaultProfileName {
    param(
        [Parameter(Mandatory=$true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory=$false)]
        [string]$FrameworkRoot = "",

        [Parameter(Mandatory=$false)]
        [string]$FallbackName = "default"
    )

    try {
        $configPath = Find-KilnProfilesConfig -ProjectRoot $ProjectRoot -FrameworkRoot $FrameworkRoot
        $config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
        if ($config.default) {
            return $config.default
        }
    } catch {
        Write-Verbose "Could not determine default profile from profiles.json: $_"
    }

    return $FallbackName
}

function Load-KilnProfile {
    param(
        [Parameter(Mandatory=$true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory=$false)]
        [string]$Profile = "default",

        [Parameter(Mandatory=$false)]
        [string]$ConfigPath = "",

        [Parameter(Mandatory=$false)]
        [string]$FrameworkRoot = ""
    )

    # Find profiles.json if not provided
    if (-not $ConfigPath) {
        $ConfigPath = Find-KilnProfilesConfig -ProjectRoot $ProjectRoot -FrameworkRoot $FrameworkRoot
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
        $workerModel = $terminal.workerModel -replace "'", "''"
        $assignments += "`$TERMINAL_${i}_ROLE = '$role';"
        $assignments += "`$TERMINAL_${i}_WORKTREE = '$worktree';"
        $assignments += "`$TERMINAL_${i}_AGENT = '$agent';"
        if ($model) {
            $assignments += "`$TERMINAL_${i}_MODEL = '$model';"
        }
        if ($workerModel) {
            $assignments += "`$TERMINAL_${i}_WORKER_MODEL = '$workerModel';"
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


