#!/usr/bin/env pwsh
# Profile Loader Module for Kiln
# Loads YAML profiles and extracts terminal/agent configuration

<#
.SYNOPSIS
Load a Kiln configuration profile into environment variables.

.DESCRIPTION
Reads kiln.profiles.yaml and sets TERMINAL_* environment variables for the selected profile.
Supports cascading lookup: project-local > user config > system config.

.PARAMETER ProjectRoot
Path to the project root where profiles.yaml is located.

.PARAMETER Profile
Name of the profile to load (default: 'dev').

.PARAMETER ConfigPath
Optional path to profiles.yaml. If not provided, searches standard locations.
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

    # Find profiles.yaml if not provided
    if (-not $ConfigPath) {
        $searchPaths = @(
            (Join-Path $ProjectRoot "kiln.profiles.yaml"),
            (Join-Path $ProjectRoot "kiln" "profiles.yaml"),
            (Join-Path $ProjectRoot ".kiln" "profiles.yaml")
        )

        # Add framework root if provided
        if ($FrameworkRoot) {
            $searchPaths += (Join-Path $FrameworkRoot "kiln" "profiles.yaml")
        }

        # Add user and system paths
        $searchPaths += @(
            (Join-Path $env:USERPROFILE ".kiln" "profiles.yaml"),
            "C:\ProgramData\kiln\profiles.yaml"
        )

        foreach ($path in $searchPaths) {
            if (Test-Path $path) {
                $ConfigPath = $path
                Write-Verbose "Found profiles.yaml at: $ConfigPath"
                break
            }
        }

        if (-not $ConfigPath) {
            throw "Could not find profiles.yaml. Searched: $($searchPaths -join ', ')"
        }
    }

    # Load and parse YAML
    Write-Verbose "Loading profile '$Profile' from: $ConfigPath"
    $yaml = Get-Content -Path $ConfigPath -Raw
    $config = ConvertFrom-Yaml $yaml

    # Find the selected profile
    if (-not $config.profiles.ContainsKey($Profile)) {
        $availableProfiles = $config.profiles.Keys -join ", "
        throw "Profile '$Profile' not found. Available profiles: $availableProfiles"
    }

    $selectedProfile = $config.profiles[$Profile]
    Write-Verbose "Loaded profile: $Profile"
    Write-Verbose "Description: $($selectedProfile.description)"

    # Output variable assignments that can be evaluated in the caller's scope
    $assignments = @()
    $desc = $selectedProfile.description -replace "'", "''"
    $assignments += "`$PROFILE_NAME = '$Profile';"
    $assignments += "`$PROFILE_DESCRIPTION = '$desc';"
    $assignments += "`$TERMINAL_COUNT = $($selectedProfile.terminals.Count);"

    for ($i = 0; $i -lt $selectedProfile.terminals.Count; $i++) {
        $terminal = $selectedProfile.terminals[$i]
        $role = $terminal.role -replace "'", "''"
        $worktree = $terminal.worktree -replace "'", "''"
        $agent = $terminal.agent -replace "'", "''"
        $assignments += "`$TERMINAL_${i}_ROLE = '$role';"
        $assignments += "`$TERMINAL_${i}_WORKTREE = '$worktree';"
        $assignments += "`$TERMINAL_${i}_AGENT = '$agent';"
    }

    return $assignments -join " "
}

function ConvertFrom-Yaml {
    param(
        [Parameter(Mandatory=$true)]
        [string]$YamlContent
    )

    # Simple YAML parser for Kiln profiles
    $result = @{ profiles = @{} }
    $currentProfile = $null
    $currentTerminal = $null
    $profileName = $null
    $inTerminals = $false

    $lines = $YamlContent -split "`n"
    $lineNum = 0
    foreach ($line in $lines) {
        $lineNum++
        $trimmed = $line.Trim()

        # Skip empty lines and comments
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }

        # Calculate indentation
        $spaces = $line.Length - $trimmed.Length
        $level = [Math]::Floor($spaces / 2)

        # Finalize profile when a new profile is encountered
        if ($trimmed -match "^(\w+):\s*$" -and $level -eq 1 -and $profileName -and $profileName -ne $matches[1]) {
            if ($currentTerminal) {
                $currentProfile.terminals += $currentTerminal
                $currentTerminal = $null
            }
            if ($profileName) {
                $result.profiles[$profileName] = $currentProfile
            }
        }

        # Parse profile definition
        if ($trimmed -match "^(\w+):\s*$" -and $level -eq 1) {
            $profileName = $matches[1]
            $currentProfile = @{
                description = ""
                terminals = @()
                messageBackend = "sqlite"
                logLevel = "info"
                env = @{}
            }
            $inTerminals = $false
            continue
        }

        # Parse profile properties at level 2
        if ($profileName -and $level -eq 2) {
            if ($trimmed -match '^description:\s*"?(.+?)"?\s*$') {
                $currentProfile.description = $matches[1]
            }
            elseif ($trimmed -match '^messageBackend:\s*(\w+)') {
                $currentProfile.messageBackend = $matches[1]
            }
            elseif ($trimmed -match '^logLevel:\s*(\w+)') {
                $currentProfile.logLevel = $matches[1]
            }
            elseif ($trimmed -eq "terminals:") {
                $inTerminals = $true
            }
            elseif ($trimmed -eq "env:") {
                $inTerminals = $false
            }
        }

        # Parse terminal list items (role at level 3, properties at level 4)
        if ($inTerminals) {
            if ($level -eq 3 -and $trimmed -match "^-\s+role:\s*(.+)$") {
                if ($currentTerminal) {
                    $currentProfile.terminals += $currentTerminal
                }
                $currentTerminal = @{ role = $matches[1].Trim(); agent = ""; worktree = ""; title = "" }
            }
            elseif ($level -eq 4 -and $currentTerminal) {
                if ($trimmed -match "^agent:\s*(.+)$") {
                    $currentTerminal.agent = $matches[1].Trim().Trim('"').Trim("'")
                }
                elseif ($trimmed -match "^worktree:\s*(.+)$") {
                    $currentTerminal.worktree = $matches[1].Trim().Trim('"').Trim("'")
                }
                elseif ($trimmed -match "^title:\s*(.+)$") {
                    $currentTerminal.title = $matches[1].Trim().Trim('"').Trim("'")
                }
            }
        }

        # Parse environment variables at level 3
        if (-not $inTerminals -and $level -eq 3 -and $profileName) {
            if ($trimmed -match '^(\w+):\s*"?(.+?)"?\s*$') {
                $varName = $matches[1]
                $varValue = $matches[2]
                if ($varName -notin @("description", "terminals", "messageBackend", "logLevel")) {
                    $currentProfile.env[$varName] = $varValue
                }
            }
        }
    }

    # Add final profile and terminal
    if ($currentTerminal) {
        $currentProfile.terminals += $currentTerminal
    }
    if ($profileName) {
        $result.profiles[$profileName] = $currentProfile
    }

    return $result
}


