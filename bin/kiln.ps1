#!/usr/bin/env pwsh
# Kiln Windows Entry Point
# Orchestrates multi-agent development on Windows Terminal or WezTerm with Claude Code skills

param(
    [string]$WorkingDir = (Get-Location).Path,
    [string]$Terminal = $null,
    [string]$ProfileName = $null,
    [switch]$Debug = $false,
    [switch]$Stop = $false
)

$ErrorActionPreference = "Stop"

# Constants
$SESSION_PREFIX = "kiln"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$KILN_BUNDLED_DIR = Join-Path (Split-Path -Parent $SCRIPT_DIR) "kiln"
# Note: KILN_DIR, STATE_DIR, WORKTREES_DIR are initialized after WorkingDir is resolved to absolute path (see line 584)

# Note: Color variables removed; using Write-Host -ForegroundColor instead for cross-platform compatibility

# State arrays (global scope so functions can modify them)
$global:ROLES = @()
$global:AGENTS = @()
$global:MODES = @()
$global:DISPLAY_NAMES = @()
$global:WORKTREE_NAMES = @()
$global:WORKTREE_PATHS = @()
$global:ROLE_INDEX = @{}

function Test-Dependency {
    param([string]$Command)
    $result = Get-Command $Command -ErrorAction SilentlyContinue
    if ($result) {
        return $true
    }
    Write-Host "Error: '$Command' is required but not installed." -ForegroundColor Red
    exit 1
}

function Get-TerminalBackend {
    # Priority: parameter > env var > auto-detect
    if ($Terminal) {
        return $Terminal.ToLower()
    }

    if ($env:KILN_TERMINAL) {
        return $env:KILN_TERMINAL.ToLower()
    }

    # Auto-detect: WezTerm if running inside it
    if ($env:WEZTERM_PANE -and (Get-Command wezterm -ErrorAction SilentlyContinue)) {
        return "wezterm"
    }

    # Prefer WezTerm if available, fall back to Windows Terminal
    if (Get-Command wezterm -ErrorAction SilentlyContinue) {
        return "wezterm"
    }

    return "wt"
}

function Import-TerminalAdapter {
    param([string]$Backend)

    $adapterPath = Join-Path $SCRIPT_DIR ".." "lib" "terminal-adapters" "$Backend.ps1"

    if (!(Test-Path $adapterPath)) {
        Write-Host "Error: Unknown terminal backend '$Backend'. Expected adapter file: $adapterPath" -ForegroundColor Red
        exit 1
    }

    # Dot-source in the caller's scope so functions are available globally
    . $adapterPath @args
}

function Get-ClaudeConfigJson {
    $frameworkRoot = Split-Path -Parent $SCRIPT_DIR
    $templatePath = Join-Path $frameworkRoot "kiln" ".claude" "settings.json"
    if (-not (Test-Path $templatePath)) {
        Write-Host "Error: Claude settings template not found at: $templatePath" -ForegroundColor Red
        exit 1
    }
    return (Get-Content -Path $templatePath -Raw)
}

function Write-DirectoryGitignore {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) {
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    }
    $path = Join-Path $Dir ".gitignore"
    if (-not (Test-Path $path)) {
        Set-Content -Path $path -Value "*" -Encoding UTF8
    }
}

function Ensure-InitialGitignore {
    $gitignorePath = Join-Path $WorkingDir ".gitignore"
    if (Test-Path $gitignorePath) {
        # Ensure required patterns are in .gitignore (older kiln-init runs may predate some of these)
        $content = Get-Content $gitignorePath -Raw
        # No trailing slash on .kiln: Prepare-Worktrees creates .kiln as a symlink in each
        # worktree, and a trailing-slash gitignore pattern only matches real directories,
        # not symlinks — with the slash, the symlink stays untracked-but-not-ignored and
        # can get swept into a commit, later breaking merges that try to check it out.
        $needsKiln = $content -notmatch '^\s*\.kiln\s*$'
        $needsWorktrees = $content -notmatch '^\s*\.worktrees/\s*$'
        $needsGithub = $content -notmatch '^\s*\.github/\s*$'
        $needsClaudeSkills = $content -notmatch '^\s*\.claude/skills\s*$'
        # CLAUDE.md, .mcp.json, and tmp/ are regenerated per-worktree/per-role with different
        # content each time (Write-GeneratedCLAUDEmd, Prepare-Worktrees). If tracked, every
        # role's copy differs, so every /kiln-receive merge hits an add/add conflict on all
        # three — confirmed live: an agent got stuck exactly here and had to escalate instead
        # of resolving it, since these files were never supposed to be shared across roles.
        $needsClaudeMd = $content -notmatch '^\s*CLAUDE\.md\s*$'
        $needsMcpJson = $content -notmatch '^\s*\.mcp\.json\s*$'
        $needsTmp = $content -notmatch '^\s*tmp/\s*$'

        if ($needsKiln) { Add-Content $gitignorePath ".kiln" -Encoding UTF8 }
        if ($needsWorktrees) { Add-Content $gitignorePath ".worktrees/" -Encoding UTF8 }
        if ($needsGithub) { Add-Content $gitignorePath ".github/" -Encoding UTF8 }
        if ($needsClaudeSkills) { Add-Content $gitignorePath ".claude/skills" -Encoding UTF8 }
        if ($needsClaudeMd) { Add-Content $gitignorePath "CLAUDE.md" -Encoding UTF8 }
        if ($needsMcpJson) { Add-Content $gitignorePath ".mcp.json" -Encoding UTF8 }
        if ($needsTmp) { Add-Content $gitignorePath "tmp/" -Encoding UTF8 }
    } else {
        # Create new .gitignore with essential patterns
        $gitignoreContent = @'
.DS_Store
.env
.env.local
*.pyc
__pycache__/
*.egg-info/
dist/
build/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/
.venv/
venv/
.idea/
.vscode/
*.swp
*.swo
*~
.kiln
.worktrees/
.github/
.claude/skills
CLAUDE.md
.mcp.json
tmp/
'@
        Set-Content -Path $gitignorePath -Value $gitignoreContent -Encoding UTF8
    }
}

function Initialize-GitRepo {
    $isNewRepo = -not (Test-Path (Join-Path $WorkingDir ".git"))

    if ($isNewRepo) {
        git -C $WorkingDir init | Out-Null
        git -C $WorkingDir branch -M main | Out-Null
    }

    Ensure-InitialGitignore

    if ($isNewRepo) {
        git -C $WorkingDir add . | Out-Null
        git -C $WorkingDir commit -m "Initial kiln repository" 2>&1 | Out-Null

        # Verify initial commit was created
        $headExists = git -C $WorkingDir rev-parse HEAD 2>$null
        if (-not $headExists) {
            Write-Host "Error: Initial git commit failed. Make sure git is configured correctly." -ForegroundColor Red
            Write-Host "Try running: git config --global user.email 'test@example.com' && git config --global user.name 'Test User'" -ForegroundColor Yellow
            exit 1
        }
    } else {
        # kiln-init.ps1 always creates .git itself, so this branch is what actually runs for
        # every real kiln project — the "$isNewRepo" branch above almost never fires in
        # practice. If .gitignore was never committed (kiln-init.ps1 writes it but deliberately
        # leaves the first commit to the user), commit it now, before Prepare-Worktrees creates
        # any worktree branches from HEAD. Otherwise `git worktree add` won't check it out, the
        # .kiln symlink (and anything else meant to be ignored) stays unprotected in each
        # worktree, and the next broad `git add` there can accidentally track it — which later
        # breaks merges that try to check that tracked symlink out over a locked messages.db.
        $gitignoreTracked = git -C $WorkingDir ls-files --error-unmatch .gitignore 2>$null
        if (-not $gitignoreTracked) {
            git -C $WorkingDir add .gitignore | Out-Null
            git -C $WorkingDir commit -m "kiln: ensure .gitignore is tracked before creating worktrees" 2>&1 | Out-Null
        }
    }
}

function Install-GitHooks {
    $hookPath = Join-Path $WorkingDir ".git\hooks\pre-push"
    if (Test-Path $hookPath) { return }

    New-Item -ItemType Directory -Force -Path (Split-Path $hookPath) | Out-Null

    $hookContent = @'
#!/bin/sh
BRANCH_LIST="$(git rev-parse --git-dir)/kiln-sub-branches"
while read local_ref local_sha remote_ref remote_sha; do
  branch="${local_ref#refs/heads/}"
  if [ -f "$BRANCH_LIST" ] && grep -qxF "$branch" "$BRANCH_LIST"; then
    echo "error: '$branch' is a Kiln sub-branch and cannot be pushed."
    exit 1
  fi
done
exit 0
'@
    Set-Content -Path $hookPath -Value $hookContent -Encoding UTF8
}

function Display-NameForRole {
    param([string]$Role)
    $parts = $Role -split "[-_]"
    $label = ($parts | ForEach-Object { $_.Substring(0, 1).ToUpper() + $_.Substring(1).ToLower() }) -join " "
    return $label
}

function Session-NameForRole {
    param([string]$Role)
    return "${SESSION_PREFIX}-$Role"
}

function Worktree-PathForName {
    param([string]$Name)
    $basePath = (Resolve-Path $WORKTREES_DIR -ErrorAction SilentlyContinue).Path
    if (-not $basePath) {
        $basePath = $WORKTREES_DIR
    }
    return Join-Path $basePath $Name
}

function Load-ConfigFromProfile {
    # Extract window entries from profile variables set by load_kiln_profile
    $i = 0
    while ($i -lt $TERMINAL_COUNT) {
        $varRole = "TERMINAL_${i}_ROLE"
        $varWorktree = "TERMINAL_${i}_WORKTREE"
        $varAgent = "TERMINAL_${i}_AGENT"

        $role = Get-Variable -Name $varRole -ValueOnly -ErrorAction SilentlyContinue
        $worktree = Get-Variable -Name $varWorktree -ValueOnly -ErrorAction SilentlyContinue
        $agent = Get-Variable -Name $varAgent -ValueOnly -ErrorAction SilentlyContinue

        if (-not [string]::IsNullOrEmpty($role)) {
            # Default to claude if agent not specified in profile
            if ([string]::IsNullOrEmpty($agent)) {
                $agent = "claude"
            }

            if ($global:ROLE_INDEX.ContainsKey($role)) {
                Write-Host "Error: Duplicate role '$role'" -ForegroundColor Red
                exit 1
            }

            if ($agent -notin @("claude", "copilot", "codex", "grok")) {
                Write-Host "Error: Unsupported agent '$agent' for role '$role'" -ForegroundColor Red
                exit 1
            }

            $global:ROLE_INDEX[$role] = $global:ROLES.Count
            $global:ROLES += $role
            $global:AGENTS += $agent
            $varMode = "TERMINAL_${i}_MODE"
            $mode = Get-Variable -Name $varMode -ValueOnly -ErrorAction SilentlyContinue
            if ([string]::IsNullOrEmpty($mode)) { $mode = "auto" }
            $global:MODES += $mode
            $global:DISPLAY_NAMES += (Display-NameForRole $role)
            $global:WORKTREE_NAMES += $worktree

            if ($worktree -eq "none" -or $worktree -eq "master" -or $worktree -eq "@current") {
                $global:WORKTREE_PATHS += $WorkingDir
            } else {
                $global:WORKTREE_PATHS += (Worktree-PathForName $worktree)
            }
        }
        $i++
    }

    if ($global:ROLES.Count -eq 0) {
        Write-Host "Error: No windows defined in profile" -ForegroundColor Red
        exit 1
    }
}

function Write-SessionsFile {
    $content = @()
    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $index = $i + 1
        $line = "{0}`t{1}`t{2}`t{3}" -f $index, $global:ROLES[$i], $global:AGENTS[$i], $global:DISPLAY_NAMES[$i]
        $content += $line
    }
    Set-Content $SESSIONS_FILE $content
}

function Write-ClaudeConfig {
    $claudeDir = Join-Path $WorkingDir ".claude"
    New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
    Write-DirectoryGitignore $claudeDir

    # Copy template settings.json from framework
    $frameworkRoot = Split-Path -Parent $PSScriptRoot
    $templateSettings = Join-Path $frameworkRoot "kiln\.claude\settings.json"
    $targetSettings = Join-Path $claudeDir "settings.json"

    if (Test-Path $templateSettings) {
        Copy-Item -Path $templateSettings -Destination $targetSettings -Force
    } else {
        Write-Host "Warning: Could not find Claude settings template at $templateSettings" -ForegroundColor Yellow
    }

    # Generate .mcp.json in project root for @current roles
    $projectMcp = Join-Path $WorkingDir ".mcp.json"
    $dbPath = Join-Path $STATE_DIR "messages.db"
    $dbPathEscaped = $dbPath -replace '\\', '\\'
    $channelScript = Join-Path $frameworkRoot "kiln" "mcp-server" "channel.py"
    $channelScriptEscaped = $channelScript -replace '\\', '\\'

    # Find the first @current role — that role gets kiln-channel in the root .mcp.json
    $currentRole = ""
    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $wt = $global:WORKTREE_NAMES[$i]
        if ($wt -eq "@current" -or $wt -eq "none" -or $wt -eq "master") {
            $currentRole = $global:ROLES[$i]
            break
        }
    }

    if ($currentRole) {
        $logsDir = Join-Path $STATE_DIR "logs"
        New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
        $statusDir = Join-Path $STATE_DIR "status"
        New-Item -ItemType Directory -Force -Path $statusDir | Out-Null
        # Seed framework tools into project state directory so agents can invoke set-status.py
        $frameworkToolsDir = Join-Path $frameworkRoot "kiln" "tools"
        $stateToolsDir = Join-Path $STATE_DIR "tools"
        if (Test-Path $frameworkToolsDir) {
            New-Item -ItemType Directory -Force -Path $stateToolsDir | Out-Null
            Copy-Item -Path (Join-Path $frameworkToolsDir "*") -Destination $stateToolsDir -Recurse -Force
        }
        $channelLog = Join-Path $logsDir "channel-$currentRole.log"
        $channelLogEscaped = $channelLog -replace '\\', '\\'
        $mcpContent = @"
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$dbPathEscaped"]
    },
    "kiln-channel": {
      "command": "python",
      "args": ["$channelScriptEscaped"],
      "env": {
        "KILN_ROLE": "$currentRole",
        "KILN_DB_PATH": "$dbPathEscaped",
        "KILN_BRANCH": "$CurrentBranch",
        "KILN_CHANNEL_LOG": "$channelLogEscaped"
      }
    }
  }
}
"@
    } else {
        $mcpContent = @"
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$dbPathEscaped"]
    }
  }
}
"@
    }
    Set-Content -Path $projectMcp -Value $mcpContent -Encoding UTF8
}

function Prepare-Workspace {
    New-Item -ItemType Directory -Force -Path $STATE_DIR, $WORKTREES_DIR | Out-Null
    Write-DirectoryGitignore $STATE_DIR
    Write-DirectoryGitignore $WORKTREES_DIR
    Write-DirectoryGitignore $KILN_DIR

    Write-ClaudeConfig
}


function Prepare-Worktrees {
    # Clean up any broken worktree references from previous runs
    git -C $WorkingDir worktree prune 2>$null

    $subBranchList = Join-Path $WorkingDir ".git\kiln-sub-branches"
    Set-Content -Path $subBranchList -Value "" -Encoding UTF8

    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $worktreeName = $global:WORKTREE_NAMES[$i]
        $worktreePath = $global:WORKTREE_PATHS[$i]
        $role = $global:ROLES[$i]

        if ($worktreeName -eq "none" -or $worktreeName -eq "master" -or $worktreeName -eq "@current") {
            continue
        }

        $branchName = "$CurrentBranch-$worktreeName"

        # Ensure path is absolute
        if (-not [System.IO.Path]::IsPathRooted($worktreePath)) {
            $worktreePath = Join-Path $WorkingDir $worktreePath
        }

        if (!(Test-Path $worktreePath)) {
            $output = git -C $WorkingDir worktree add --force -B $branchName $worktreePath HEAD 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Error creating worktree for role '$role': $output" -ForegroundColor Red
                exit 1
            }
        }

        # Set up symlink to shared .kiln directory for direct database access
        $worktreeKilnDir = Join-Path $worktreePath ".kiln"

        # Remove old directory structure if it exists (migration from previous versions)
        if (Test-Path $worktreeKilnDir) {
            Remove-Item -Path $worktreeKilnDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        # Create symlink to shared .kiln directory (use relative path for portability)
        $relativeStatePath = [System.IO.Path]::GetRelativePath($worktreePath, $STATE_DIR)
        try {
            New-Item -ItemType SymbolicLink -Path $worktreeKilnDir -Target $relativeStatePath -Force -ErrorAction Stop | Out-Null
            Write-Host "    [$role] ✓ Symlinked .kiln → $relativeStatePath" -ForegroundColor Green
        } catch {
            Write-Host "    [$role] Warning: Could not create symlink: $_" -ForegroundColor Yellow
            Write-Host "    [$role] Falling back to directory copy" -ForegroundColor Yellow
            # Fallback: copy the directory structure instead
            New-Item -ItemType Directory -Force -Path $worktreeKilnDir | Out-Null
            $mainToolsDir = Join-Path $STATE_DIR "tools"
            $worktreeToolsDir = Join-Path $worktreeKilnDir "tools"
            if ((Test-Path $mainToolsDir) -and -not (Test-Path $worktreeToolsDir)) {
                Copy-Item -Path $mainToolsDir -Destination $worktreeToolsDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            $worktreeLogsDir = Join-Path $worktreeKilnDir "logs"
            New-Item -ItemType Directory -Force -Path $worktreeLogsDir | Out-Null
        }

        # Copy Claude config to worktree
        $worktreeClaudeDir = Join-Path $worktreePath ".claude"
        New-Item -ItemType Directory -Force -Path $worktreeClaudeDir | Out-Null
        $worktreeSettingsFile = Join-Path $worktreeClaudeDir "settings.json"
        $json = Get-ClaudeConfigJson
        Set-Content -Path $worktreeSettingsFile -Value $json -Encoding UTF8

        # Copy worker agent definitions into worktree's .claude/agents/ so Claude Code can find them
        $worktreeAgentsDir = Join-Path $worktreeClaudeDir "agents"
        New-Item -ItemType Directory -Force -Path $worktreeAgentsDir | Out-Null
        $projectAgentsDir = Join-Path $WorkingDir ".claude" "agents"
        if (Test-Path $projectAgentsDir) {
            Get-ChildItem -Path $projectAgentsDir -Filter "*-worker.md" | ForEach-Object {
                Copy-Item -Path $_.FullName -Destination $worktreeAgentsDir -Force
            }
        }

        # Create .mcp.json in worktree root with MCP server configuration
        $claudeJsonPath = Join-Path $worktreePath ".mcp.json"
        $dbPath = Join-Path $STATE_DIR "messages.db"
        $dbPathEscaped = $dbPath -replace '\\', '\\'
        $channelScript = Join-Path (Split-Path -Parent $SCRIPT_DIR) "kiln" "mcp-server" "channel.py"
        $channelScriptEscaped = $channelScript -replace '\\', '\\'
        $logsDir = Join-Path $STATE_DIR "logs"
        New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
        $statusDir = Join-Path $STATE_DIR "status"
        New-Item -ItemType Directory -Force -Path $statusDir | Out-Null
        $channelLog = Join-Path $logsDir "channel-$role.log"
        $channelLogEscaped = $channelLog -replace '\\', '\\'
        $claudeJson = @"
{
  "name": "kiln-$role",
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$dbPathEscaped"]
    },
    "kiln-channel": {
      "command": "python",
      "args": ["$channelScriptEscaped"],
      "env": {
        "KILN_ROLE": "$role",
        "KILN_DB_PATH": "$dbPathEscaped",
        "KILN_BRANCH": "$CurrentBranch",
        "KILN_CHANNEL_LOG": "$channelLogEscaped"
      }
    }
  }
}
"@
        Set-Content -Path $claudeJsonPath -Value $claudeJson -Encoding UTF8
        Write-Verbose "Created .mcp.json in worktree $role (with kiln-channel)"

        # Create tmp directory for handoff files
        $tmpDir = Join-Path $worktreePath "tmp"
        New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

        Add-Content -Path $subBranchList -Value $branchName
    }
}

function Prepare-Skills {
    $skillsSource = Join-Path $KILN_DIR "skills"
    if (-not (Test-Path $skillsSource)) {
        Write-Host "  [skills] No skills directory found at: $skillsSource" -ForegroundColor Gray
        return
    }

    $skillDirs = @(Get-ChildItem $skillsSource -Directory -ErrorAction SilentlyContinue)
    if ($skillDirs.Count -eq 0) {
        Write-Host "  [skills] Skills directory exists but is empty" -ForegroundColor Gray
        return
    }

    Write-Host "  [skills] Found $($skillDirs.Count) skill(s) in $skillsSource" -ForegroundColor Cyan

    # Prepare skills for all agents (Claude and Copilot)
    $pathsToProcess = @()
    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $agent = $global:AGENTS[$i]
        if ($agent -eq "claude" -or $agent -eq "copilot") {
            $pathsToProcess += @{ Role = $global:ROLES[$i]; Agent = $agent; Path = $global:WORKTREE_PATHS[$i] }
        }
    }

    # Also add root working directory
    if ($pathsToProcess.Count -gt 0) {
        $pathsToProcess += @{ Role = "root"; Agent = "claude"; Path = $WorkingDir }
    }

    foreach ($item in $pathsToProcess) {
        $role = $item.Role
        $agent = $item.Agent
        $worktreePath = $item.Path

        # Determine skills directory based on agent type
        $skillsDir = if ($agent -eq "copilot") {
            Join-Path $worktreePath ".github" "skills"
        } else {
            Join-Path $worktreePath ".claude" "skills"
        }

        # Always recreate so removed skills don't linger
        if (Test-Path $skillsDir) {
            Remove-Item $skillsDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null

        foreach ($skillDir in $skillDirs) {
            $target = Join-Path $skillsDir $skillDir.Name
            try {
                New-Item -ItemType Junction -Path $target -Target $skillDir.FullName -ErrorAction Stop | Out-Null
                Write-Host "    [$role] → $($skillDir.Name)" -ForegroundColor Green
            } catch {
                Write-Host "    [$role] ✗ Failed to link $($skillDir.Name): $_" -ForegroundColor Red
            }
        }
    }
}

function Prepare-AgentConfigs {
    # Create ~/.copilot/mcp-config.json for Copilot agents (if any exist in the swarm)
    $dbPath = Join-Path $STATE_DIR "messages.db"
    $dbPathEscaped = $dbPath -replace '\\', '\\'

    $hasCopilotAgent = $false
    for ($i = 0; $i -lt $global:AGENTS.Count; $i++) {
        if ($global:AGENTS[$i] -eq "copilot") {
            $hasCopilotAgent = $true
            break
        }
    }

    if ($hasCopilotAgent) {
        $copilotDir = Join-Path $env:USERPROFILE ".copilot"
        New-Item -ItemType Directory -Path $copilotDir -Force | Out-Null

        $mcpConfigPath = Join-Path $copilotDir "mcp-config.json"
        $mcpConfig = @"
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$dbPathEscaped"]
    }
  }
}
"@
        Set-Content -Path $mcpConfigPath -Value $mcpConfig -Encoding UTF8
        Write-Host "Created ~/.copilot/mcp-config.json" -ForegroundColor Green
    }
}

function Get-KilnTemplate([string]$Name) {
    Get-Content (Join-Path $KILN_BUNDLED_DIR "templates" "$Name.md") -Raw -ErrorAction Stop
}

function Get-KilnConstitution([string]$Name) {
    Get-Content (Join-Path $KILN_DIR "constitution" "$Name.md") -Raw -ErrorAction Stop
}

function Get-KilnConstitutionHeader {
    $path = Join-Path $KILN_DIR "constitution.md"
    if (Test-Path $path) { Get-Content $path -Raw } else { $null }
}

function Get-KilnRole([string]$Role) {
    $raw = Get-Content (Join-Path $KILN_DIR "roles" "$Role.md") -Raw -ErrorAction Stop
    $stripped = ($raw -replace '(?ms)^## (Message Loop|Interaction Loop).*?(?=^## |\z)', '').TrimStart("`r", "`n")
    "# Role`n`n" + $stripped
}

function Apply-Substitutions([string]$Text, [hashtable]$Map) {
    foreach ($key in $Map.Keys) {
        $Text = $Text.Replace($key, $Map[$key])
    }
    $Text
}

function Read-HandoffRoutingTable([string]$Path) {
    $table = @{}
    $fileContent = Get-Content $Path -Raw
    $tableMatches = [regex]::Matches($fileContent, '(?m)^\|\s*(\w+)\s*\|\s*(\w+)\s*\|')
    foreach ($m in $tableMatches) {
        $role   = $m.Groups[1].Value
        $target = $m.Groups[2].Value
        if ($role -ne 'Role') { $table[$role] = $target }
    }
    return $table
}

function Write-GeneratedCLAUDEmd {
    param(
        [int]$Index,
        [string]$Role,
        [string]$WorktreePath,
        [string]$Agent = "claude",
        [string]$Mode  = "auto"
    )

    # Resolve output path
    if ($Agent -eq "copilot") {
        $instructionsDir = Join-Path $WorktreePath ".github"
        New-Item -ItemType Directory -Force -Path $instructionsDir | Out-Null
        $outPath = Join-Path $instructionsDir "copilot-instructions.md"
    } else {
        $outPath = Join-Path $WorktreePath "CLAUDE.md"
    }
    New-Item -ItemType Directory -Force -Path $WorktreePath | Out-Null

    # Load template blocks in order: wrapper-prompt (auto-mode only) -> loop -> runtime -> constitution -> role
    $wrapperPromptBlock = if ($Mode -eq "auto" -and $Agent -eq "claude") { Get-KilnTemplate "wrapper-prompt-auto-$Agent" } else { $null }
    $loopBlock         = Get-KilnTemplate "loop-$Mode-$Agent"
    $runtimeBlock      = Get-KilnTemplate "runtime-$Agent"
    $constitutionBlock = Get-KilnConstitutionHeader
    $workflow          = Get-KilnConstitution "workflow"
    $engineering       = Get-KilnConstitution "engineering"
    $project           = Get-KilnConstitution "project"
    $roleBlock         = Get-KilnRole $Role

    # Build substitution map
    $dbPath = Join-Path $STATE_DIR "messages.db"
    $subs = @{
        "{{ROLE}}"           = $Role
        "{{ROLE_UPPER}}"     = $Role.ToUpper()
        "{{BRANCH}}"         = $CurrentBranch
        "{{DB_PATH}}"        = $dbPath
        "{{WORKTREE}}"       = (Split-Path -Leaf $WorktreePath)
        "{{HANDOFF_TARGET}}" = if ($HandoffTargets.ContainsKey($Role)) { $HandoffTargets[$Role] } else { "specifier" }
        "{{COMMIT_FORMAT}}"  = if ($CommitFormats.ContainsKey($Role)) { $CommitFormats[$Role] } else { "[$Role] <description>" }
    }

    # Render each block, join with horizontal rules.
    # For 'auto' mode roles (Claude agents with subagent delegation), exclude the role block
    # since that's now in the worker subagent. Also exclude project/engineering (they're in the
    # worker file and duplicated here; the shell never does implementation work). For 'manual'
    # mode (e.g., specifier), include the role block and full constitution.
    if ($Mode -eq "auto" -and $Agent -eq "claude") {
        $blocks = @($wrapperPromptBlock, $loopBlock, $runtimeBlock, $workflow) | Where-Object { $_ }
    } else {
        $blocks = @($roleBlock, $loopBlock, $runtimeBlock, $constitutionBlock, $project, $engineering, $workflow) | Where-Object { $_ }
    }
    $body   = ($blocks | ForEach-Object { Apply-Substitutions $_ $subs }) -join "`n`n---`n`n"

    # Write with auto-gen header
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $header  = "<!-- Auto-generated by kiln.ps1 on $timestamp -->`n"
    $header += "<!-- DO NOT EDIT MANUALLY -->`n`n"
    Set-Content $outPath ($header + $body) -Encoding UTF8
}

function Write-GeneratedWorkerAgent {
    param(
        [string]$Role
    )

    # The worker subagent does the role's actual implementation work for one cycle,
    # dispatched by the persistent shell agent's loop template (Step 2). It gets the
    # role + engineering/project constitution, but not workflow.md (handoff/messaging
    # protocol stays the shell's concern) and no MCP/Agent tools (no messaging, no
    # recursive subagent spawning).
    #
    # Generated in the main project's .claude/agents/ directory (not the worktree's),
    # so Claude Code's agent discovery finds it when the shell agent (running in the
    # worktree) spawns the subagent via the Agent tool.
    $agentsDir = Join-Path $WorkingDir ".claude" "agents"
    New-Item -ItemType Directory -Force -Path $agentsDir | Out-Null
    $outPath = Join-Path $agentsDir "$Role-worker.md"

    $roleBlock   = Get-KilnRole $Role
    $engineering = Get-KilnConstitution "engineering"
    $project     = Get-KilnConstitution "project"

    $subs = @{
        "{{ROLE}}"       = $Role
        "{{ROLE_UPPER}}" = $Role.ToUpper()
    }

    $blocks = @($roleBlock, $project, $engineering) | Where-Object { $_ }
    $body   = ($blocks | ForEach-Object { Apply-Substitutions $_ $subs }) -join "`n`n---`n`n"

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $frontmatter  = "---`n"
    $frontmatter += "name: $Role-worker`n"
    $frontmatter += "description: Performs the $Role role's implementation work for one handoff cycle. Dispatched by the persistent $Role shell agent's message loop; not for direct/standalone use.`n"
    $frontmatter += "tools: Read, Write, Edit, Glob, Grep, Bash, PowerShell, Skill, NotebookEdit, TodoWrite`n"
    $frontmatter += "---`n`n"
    $frontmatter += "<!-- Auto-generated by kiln.ps1 on $timestamp -->`n"
    $frontmatter += "<!-- DO NOT EDIT MANUALLY -->`n`n"

    Set-Content $outPath ($frontmatter + $body) -Encoding UTF8
}

function Build-WezTermAgentCommand {
    param(
        [string]$Agent,
        [string]$DisplayName,
        [string]$WorktreePath,
        [string]$Model = "",
        [string]$Role = ""
    )

    if (-not $Model) {
        Write-Warning "No model specified for $DisplayName; agent command may fail"
    }

    $command = ""

    switch ($Agent) {
        "claude" {
            $debugLog = Join-Path $STATE_DIR "logs" "claude-debug-$Role.log"
            $command = "claude --model $Model --permission-mode bypassPermissions --mcp-config ./.mcp.json --debug-file '$debugLog' -n '$DisplayName' 'Start your role session.'"
        }
        "copilot" {
            $command = "copilot --allow-all"
        }
        default {
            $command = "echo 'Agent $Agent not supported'"
        }
    }

    return $command
}

function Build-WindowsTerminalLayout {
    param(
        [string]$LayoutJson,
        [hashtable]$RoleIndex,
        [array]$Roles,
        [array]$Agents,
        [array]$DisplayNames,
        [array]$WorktreePaths
    )

    if (-not $LayoutJson) {
        # Default to tabs if no layout specified
        return Build-WindowsTerminalTabsLayout -RoleIndex $RoleIndex -Roles $Roles -Agents $Agents -DisplayNames $DisplayNames -WorktreePaths $WorktreePaths
    }

    try {
        $layout = $LayoutJson | ConvertFrom-Json
    } catch {
        Write-Host "Warning: Could not parse layout JSON, defaulting to tabs" -ForegroundColor Yellow
        return Build-WindowsTerminalTabsLayout -RoleIndex $RoleIndex -Roles $Roles -Agents $Agents -DisplayNames $DisplayNames -WorktreePaths $WorktreePaths
    }

    # Handle simple "type" format
    if ($layout.type -eq "tabs") {
        return Build-WindowsTerminalTabsLayout -RoleIndex $RoleIndex -Roles $Roles -Agents $Agents -DisplayNames $DisplayNames -WorktreePaths $WorktreePaths
    }

    # Handle tabs array format
    if ($layout.tabs) {
        return Build-WindowsTerminalTabsArrayLayout -Layout $layout -RoleIndex $RoleIndex -Roles $Roles -Agents $Agents -DisplayNames $DisplayNames -WorktreePaths $WorktreePaths
    }

    # Default fallback
    return Build-WindowsTerminalTabsLayout -RoleIndex $RoleIndex -Roles $Roles -Agents $Agents -DisplayNames $DisplayNames -WorktreePaths $WorktreePaths
}

function Build-WindowsTerminalTabsLayout {
    param(
        [hashtable]$RoleIndex,
        [array]$Roles,
        [array]$Agents,
        [array]$DisplayNames,
        [array]$WorktreePaths
    )

    $AgentColors = @("One Half Dark", "Solarized Dark", "Tango Dark", "Campbell Powershell")
    $colorEmojis = @("🟦", "🟥", "🟩", "🟨")
    $wtArgs = @()

    for ($i = 0; $i -lt $Roles.Count; $i++) {
        $agent = $Agents[$i]
        $displayName = $DisplayNames[$i]
        $worktreePath = $WorktreePaths[$i]

        # Get model from profile variable
        $varModel = "TERMINAL_${i}_MODEL"
        $model = Get-Variable -Name $varModel -ValueOnly -ErrorAction SilentlyContinue

        if ($i -eq 0) {
            $wtArgs += "new-tab"
        } else {
            $wtArgs += ";", "new-tab"
        }

        # For tabs layout, use colored emoji + role name as title
        $tabTitle = "$($colorEmojis[$i % $colorEmojis.Count]) $displayName"
        $wtArgs += "--title", $tabTitle
        $wtArgs += "-d", $worktreePath
        $wtArgs += "--colorScheme", $AgentColors[$i % $AgentColors.Count]
        $wtArgs += "pwsh", "-NoExit", "-Command"

        $agentCmd = Get-WindowsTerminalAgentCommand -Agent $agent -DisplayName $displayName -WorktreePath $worktreePath -Model $model -Role $Roles[$i]
        $wtArgs += $agentCmd
    }

    return $wtArgs
}

function Build-WindowsTerminalTabsArrayLayout {
    param(
        [object]$Layout,
        [hashtable]$RoleIndex,
        [array]$Roles,
        [array]$Agents,
        [array]$DisplayNames,
        [array]$WorktreePaths
    )

    $AgentColors = @("One Half Dark", "Solarized Dark", "Tango Dark", "Campbell Powershell")
    $wtArgs = @()

    foreach ($tabIndex in 0..($Layout.tabs.Count - 1)) {
        $tab = $Layout.tabs[$tabIndex]

        if ($tabIndex -eq 0) {
            $wtArgs += "new-tab"
        } else {
            $wtArgs += ";", "new-tab"
        }

        # Set tab title only once per tab (for multi-pane tabs)
        if ($tab.title) {
            $wtArgs += "--title", $tab.title
        } else {
            # Generate title from all role names in this tab: "Role1 & Role2 & Role3"
            $roleNames = @()
            foreach ($pane in $tab.panes) {
                $roleInPane = $pane.role
                if ($RoleIndex.ContainsKey($roleInPane)) {
                    $roleIdx = $RoleIndex[$roleInPane]
                    $roleNames += $DisplayNames[$roleIdx]
                }
            }
            if ($roleNames.Count -gt 0) {
                $tabTitle = $roleNames -join " `& "
            } else {
                $tabTitle = "Kiln"
            }
            $wtArgs += "--title", $tabTitle
        }

        # Determine if this tab has a grid layout
        $isGrid = ($tab.gridRows -and $tab.gridCols)
        $gridCols = if ($isGrid) { $tab.gridCols } else { 1 }

        # Track current pane position for grid navigation
        $currentRow = 0
        $currentCol = 0

        # Process panes in this tab
        foreach ($paneIndex in 0..($tab.panes.Count - 1)) {
            $pane = $tab.panes[$paneIndex]
            $roleInPane = $pane.role

            if ($RoleIndex.ContainsKey($roleInPane)) {
                $roleIdx = $RoleIndex[$roleInPane]
                $agent = $Agents[$roleIdx]
                $displayName = $DisplayNames[$roleIdx]
                $worktreePath = $WorktreePaths[$roleIdx]

                # Get model from profile variable
                $varModel = "TERMINAL_${roleIdx}_MODEL"
                $model = Get-Variable -Name $varModel -ValueOnly -ErrorAction SilentlyContinue

                # Add pane split if not first pane in tab
                if ($paneIndex -gt 0) {
                    if ($isGrid) {
                        # Grid layout: calculate position and navigate to parent pane
                        $targetRow = [math]::Floor($paneIndex / $gridCols)
                        $targetCol = $paneIndex % $gridCols

                        # Determine parent pane position
                        if ($targetRow -eq 0) {
                            # First row: parent is previous pane in same row
                            $parentRow = 0
                            $parentCol = $targetCol - 1
                        } else {
                            # Other rows: parent is pane directly above (same column, previous row)
                            $parentRow = $targetRow - 1
                            $parentCol = $targetCol
                        }

                        # Navigate from current to parent position
                        # Move vertically first (up/down) to avoid navigating to non-existent panes
                        $rowDiff = $parentRow - $currentRow
                        $colDiff = $parentCol - $currentCol

                        for ($i = 0; $i -lt [math]::Abs($rowDiff); $i++) {
                            if ($rowDiff -gt 0) {
                                $wtArgs += ";", "move-focus", "down"
                            } else {
                                $wtArgs += ";", "move-focus", "up"
                            }
                        }

                        for ($i = 0; $i -lt [math]::Abs($colDiff); $i++) {
                            if ($colDiff -gt 0) {
                                $wtArgs += ";", "move-focus", "right"
                            } else {
                                $wtArgs += ";", "move-focus", "left"
                            }
                        }

                        # Do the split
                        if ($targetRow -eq 0) {
                            $wtArgs += ";", "split-pane", "-H"
                        } else {
                            $wtArgs += ";", "split-pane", "-V"
                        }

                        # Update current position (new pane is now active)
                        $currentRow = $targetRow
                        $currentCol = $targetCol
                    } else {
                        # Non-grid: vertical splits (top/bottom)
                        $wtArgs += ";", "split-pane", "-V"
                    }
                }

                $colorIdx = $roleIdx % $AgentColors.Count
                $agentCmd = Get-WindowsTerminalAgentCommand -Agent $agent -DisplayName $displayName -WorktreePath $worktreePath -Model $model -Role $roleInPane
                $wtArgs += "-d", $worktreePath
                $wtArgs += "--colorScheme", $AgentColors[$colorIdx]
                $wtArgs += "pwsh", "-NoExit", "-Command"
                $wtArgs += $agentCmd
            }
        }
    }

    return $wtArgs
}

function Get-WindowsTerminalAgentCommand {
    param(
        [string]$Agent,
        [string]$DisplayName,
        [string]$WorktreePath,
        [string]$Model = "",
        [string]$Role = ""
    )

    if (-not $Model) {
        Write-Warning "No model specified for $DisplayName; agent command may fail"
    }

    switch ($Agent) {
        "claude" {
            $debugLog = Join-Path $STATE_DIR "logs" "claude-debug-$Role.log"
            if ($DisplayName) {
                return "claude --model $Model --permission-mode bypassPermissions --mcp-config ./.mcp.json --debug-file ""$debugLog"" -n ""$DisplayName"" ""Start your role session."""
            } else {
                return "claude --model $Model --permission-mode bypassPermissions --mcp-config ./.mcp.json --debug-file ""$debugLog"" ""Start your role session."""
            }
        }
        "copilot" {
            if ($DisplayName) {
                return "copilot --allow-all --name ""$DisplayName"""
            } else {
                return "copilot --allow-all"
            }
        }
        default {
            return "echo 'Agent $Agent not supported'"
        }
    }
}

# Main flow
Test-Dependency git

# Detect and load terminal adapter
$TerminalBackend = Get-TerminalBackend
Write-Host "Using terminal backend: $TerminalBackend" -ForegroundColor Cyan

switch ($TerminalBackend) {
    "wt" { Test-Dependency wt }
    "wezterm" {
        Test-Dependency wezterm
        $adapterPath = Join-Path $SCRIPT_DIR ".." "lib" "terminal-adapters" "wezterm.ps1"
        if (!(Test-Path $adapterPath)) {
            Write-Host "Error: Terminal adapter not found: $adapterPath" -ForegroundColor Red
            exit 1
        }
        . $adapterPath
    }
}

if ($Stop) {
    Write-Host "Stopping Kiln MCP server processes..." -ForegroundColor Cyan
    $killed = 0

    $nodeProcs = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" |
        Where-Object { $_.CommandLine -like '*mcp-sqlite*' }
    foreach ($p in $nodeProcs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  Killed node.exe (mcp-sqlite) PID $($p.ProcessId)" -ForegroundColor Green
        $killed++
    }

    $pyProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*channel.py*' }
    foreach ($p in $pyProcs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  Killed python.exe (channel.py) PID $($p.ProcessId)" -ForegroundColor Green
        $killed++
    }

    if ($killed -eq 0) {
        Write-Host "No Kiln MCP processes found." -ForegroundColor Yellow
    } else {
        Write-Host "Killed $killed process(es)." -ForegroundColor Green
    }
    exit 0
}

$WorkingDir = (Resolve-Path $WorkingDir).Path

# Initialize directory paths now that WorkingDir is resolved to absolute path
$KILN_DIR = Join-Path $WorkingDir "kiln"
$STATE_DIR = Join-Path $WorkingDir ".kiln"

# Handoff routing table — parsed from workflow.md at startup
$HandoffTargets = Read-HandoffRoutingTable (Join-Path $KILN_DIR "constitution" "workflow.md")

# Commit format strings per role
$CommitFormats = @{
    "specifier"  = "[Specifier] <feature name> - <what was specified>"
    "coder"      = "[Coder] <feature name> - TDD implementation of <what>"
    "refactorer" = "[Refactorer] <feature name> - <quality gate results>"
    "architect"  = "[Architect] <feature name> - <structural changes made>"
    "selftest"   = "[Selftest] <what was tested>"
}
$WORKTREES_DIR = Join-Path $WorkingDir ".worktrees"

Initialize-GitRepo
Install-GitHooks
$CurrentBranch = git -C $WorkingDir rev-parse --abbrev-ref HEAD 2>$null
if (-not $CurrentBranch -or $CurrentBranch -eq "HEAD") { $CurrentBranch = "kiln" }

# Load configuration profile
if (-not $ProfileName) {
    $ProfileName = "dev"
}

Write-Host "Loading configuration profile: $ProfileName" -ForegroundColor Cyan
. "$SCRIPT_DIR\..\lib\profile-loader.ps1"
$frameworkRoot = Split-Path -Parent $SCRIPT_DIR
try {
    $profileVars = Load-KilnProfile -ProjectRoot $WorkingDir -FrameworkRoot $frameworkRoot -Profile $ProfileName
    if ($profileVars) {
        Invoke-Expression $profileVars
    } else {
        throw "Profile loader returned empty result"
    }
} catch {
    Write-Host "Error: Failed to load profile '$ProfileName': $_" -ForegroundColor Red
    exit 1
}

Load-ConfigFromProfile
Prepare-Workspace

# Generate worker agent definitions before preparing worktrees so they can be copied in
for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
    $role = $global:ROLES[$i]
    $agent = $global:AGENTS[$i]
    if ($agent -eq "claude" -and $global:MODES[$i] -eq "auto") {
        Write-GeneratedWorkerAgent -Role $role
    }
}

Prepare-Worktrees
Prepare-AgentConfigs

# Create tmp directory for handoff files (used by all agents)
$tmpDir = Join-Path $WorkingDir "tmp"
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

Prepare-Skills

# Initialize message database (ACID-compliant message queuing)
Write-Host "Initializing message database..." -ForegroundColor Cyan
$dbPath = Join-Path $WorkingDir ".kiln" "messages.db"
$dbDir = Split-Path $dbPath
New-Item -ItemType Directory -Force -Path $dbDir | Out-Null

$pythonScript = @"
import sqlite3, os
db = r'$dbPath'
conn = sqlite3.connect(db)
conn.execute('''CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), sender TEXT NOT NULL, target TEXT NOT NULL,
  priority INTEGER DEFAULT 50, status TEXT DEFAULT 'queued',
  content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
  delivered_at TEXT, acked_at TEXT, processed_at TEXT, error TEXT,
  branch TEXT NOT NULL DEFAULT 'main')''')
conn.execute('CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status)')
conn.execute('PRAGMA journal_mode=WAL')
conn.commit()
conn.close()
"@

python -c $pythonScript 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Warning: Failed to initialize database via Python, trying direct sqlite3..." -ForegroundColor Yellow
    $sqliteScript = @"
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), sender TEXT NOT NULL, target TEXT NOT NULL,
  priority INTEGER DEFAULT 50, status TEXT DEFAULT 'queued',
  content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
  delivered_at TEXT, acked_at TEXT, processed_at TEXT, error TEXT,
  branch TEXT NOT NULL DEFAULT 'main');
CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status);
PRAGMA journal_mode=WAL;
"@
    $sqliteScript | sqlite3 $dbPath
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ Initialized message database via sqlite3" -ForegroundColor Green
    } else {
        Write-Host "Error: Could not initialize database (sqlite3 not found)" -ForegroundColor Red
        Write-Host "Please install sqlite3 or Python to use Kiln messaging." -ForegroundColor Yellow
    }
} else {
    Write-Host "✓ Initialized message database" -ForegroundColor Green
}

# Persist terminal backend so the watchdog knows how to wake agents
Set-Content (Join-Path $STATE_DIR "runtime.env") "terminal=$TerminalBackend" -Encoding UTF8

Write-Host "  ╔═══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║         Kiln v0.1 Starting                    ║" -ForegroundColor Cyan
Write-Host "  ║    Disciplined agents build better software   ║" -ForegroundColor Cyan
Write-Host "  ╚═══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

Write-Host "Launching agents in Windows Terminal..." -ForegroundColor Green

if ($TerminalBackend -eq "wezterm") {
    # WezTerm: generate dynamic Lua config and launch
    $roleData = @()
    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $role = $global:ROLES[$i]
        $agent = $global:AGENTS[$i]
        $displayName = $global:DISPLAY_NAMES[$i]
        $worktreePath = $global:WORKTREE_PATHS[$i]

        # Get model for this terminal from profile
        $varModel = "TERMINAL_${i}_MODEL"
        $model = Get-Variable -Name $varModel -ValueOnly -ErrorAction SilentlyContinue

        Write-Verbose "Role: '$role', DisplayName: '$displayName', Agent: '$agent', Model: '$model'"

        Write-GeneratedCLAUDEmd -Index $i -Role $role -WorktreePath $worktreePath -Agent $agent -Mode $global:MODES[$i]
        $cmd = Build-WezTermAgentCommand -Agent $agent -DisplayName $displayName -WorktreePath $worktreePath -Model $model -Role $role

        $roleData += [PSCustomObject]@{
            role  = $role
            name  = $displayName
            agent = $agent
            path  = $worktreePath
            cmd   = $cmd
        }

        Write-Host "  [$displayName] configured" -ForegroundColor Cyan
    }

    $env:KILN_PROJECT_DIR = $WorkingDir
    Start-WezTermSession -RoleData $roleData -LayoutJson $PROFILE_LAYOUT_JSON

} else {
    # Windows Terminal (wt): use profile-based layout
    for ($i = 0; $i -lt $global:ROLES.Count; $i++) {
        $role = $global:ROLES[$i]
        $worktreePath = $global:WORKTREE_PATHS[$i]
        $agent = $global:AGENTS[$i]
        Write-GeneratedCLAUDEmd -Index $i -Role $role -WorktreePath $worktreePath -Agent $agent -Mode $global:MODES[$i]
        Write-Host "  [$($global:DISPLAY_NAMES[$i])] configured" -ForegroundColor Cyan
    }

    # Build layout from profile
    $wtArgs = Build-WindowsTerminalLayout `
        -LayoutJson $PROFILE_LAYOUT_JSON `
        -RoleIndex $global:ROLE_INDEX `
        -Roles $global:ROLES `
        -Agents $global:AGENTS `
        -DisplayNames $global:DISPLAY_NAMES `
        -WorktreePaths $global:WORKTREE_PATHS

    if ($wtArgs.Count -gt 0) {
        $argString = ($wtArgs | ForEach-Object { if ($_ -match '\s') { """$_""" } else { $_ } }) -join ' '
        Start-Process wt.exe -ArgumentList $argString -NoNewWindow | Out-Null
        # Wait for panes to stabilize to avoid race conditions on rapid restarts
        Start-Sleep -Milliseconds 1500
    }
}

Write-Host ""
Write-Host "Kiln is ready." -ForegroundColor Green
Write-Host "Working directory: $WorkingDir"
$rolesJoined = $ROLES -join ", "
Write-Host "Roles configured: $rolesJoined"
Write-Host ""
