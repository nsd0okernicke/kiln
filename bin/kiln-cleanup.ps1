#!/usr/bin/env pwsh
# Cleanup all Kiln artifacts and Windows Terminal tabs

param(
    [string]$ProjectDir = $null,
    [string]$WorkingDir = $null
)

# Support both -ProjectDir and -WorkingDir for compatibility
if ($WorkingDir) {
    $ProjectDir = $WorkingDir
} elseif (-not $ProjectDir) {
    $ProjectDir = (Get-Location).Path
}

$ErrorActionPreference = "Continue"

$STATE_DIR = Join-Path $ProjectDir ".Kiln"
$WINDOWS_FILE = Join-Path $STATE_DIR "windows.json"
$WORKTREES_DIR = Join-Path $ProjectDir ".worktrees"
$SWARM_BRANCHES_FILE = Join-Path $ProjectDir ".git" "kiln-sub-branches"

Write-Host "Cleaning up Kiln sessions in $ProjectDir" -ForegroundColor Cyan

# Step 1: Close Windows Terminal windows
Write-Host ""
Write-Host "Step 1: Closing Windows Terminal tabs..."
if (Test-Path $WINDOWS_FILE) {
    try {
        $windows = Get-Content $WINDOWS_FILE | ConvertFrom-Json
        foreach ($window in $windows) {
            try {
                Stop-Process -Id $window.PID -Force -ErrorAction SilentlyContinue
                Write-Host "  ✓ Closed Windows Terminal window (PID: $($window.PID))"
            } catch {
                # Process may already be closed
            }
        }
    } catch {
        Write-Host "  ⚠ Warning: Could not read windows.json"
    }
}

# Kill all remaining wt.exe processes (fallback)
$wtProcesses = Get-Process wt -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq "wt" }
if ($wtProcesses) {
    $wtProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Terminated remaining Windows Terminal processes"
}

# Step 2: Preserve git branches (audit trail across multiple cycles)
Write-Host ""
Write-Host "Step 2: Preserving sub-branches as audit trail..."
if (Test-Path $SWARM_BRANCHES_FILE) {
    $preservedBranches = @()
    try {
        $preservedBranches = Get-Content $SWARM_BRANCHES_FILE | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    } catch {
        Write-Host "  ⚠ Warning: Could not read Kiln-sub-branches file"
    }

    if ($preservedBranches.Count -gt 0) {
        Write-Host "  ✓ Sub-branches preserved (not deleted):"
        foreach ($branch in $preservedBranches) {
            $branch = $branch.Trim()
            if (-not [string]::IsNullOrWhiteSpace($branch)) {
                Write-Host "     - $branch"
            }
        }
        Write-Host ""
        Write-Host "  These branches are the audit trail for agent work across multiple cycles."
        Write-Host "  To delete them, run: git branch -D $($preservedBranches -join ' ')"
    }
    Remove-Item $SWARM_BRANCHES_FILE -Force -ErrorAction SilentlyContinue
}

# Clean up worktree references
git -C $ProjectDir worktree prune 2>$null
Write-Host "  ✓ Pruned stale worktree references"

# Step 3: Remove worktree directories
Write-Host ""
Write-Host "Step 3: Removing worktree directories..."
if (Test-Path $WORKTREES_DIR) {
    Remove-Item $WORKTREES_DIR -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .worktrees/ directory"
}

# Step 4: Remove generated instruction files from main working directory
Write-Host ""
Write-Host "Step 4: Removing generated instruction files..."
$claudeMdPath = Join-Path $ProjectDir "CLAUDE.md"
if (Test-Path $claudeMdPath) {
    Remove-Item $claudeMdPath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed CLAUDE.md"
}

$copilotPath = Join-Path $ProjectDir ".github" "copilot-instructions.md"
if (Test-Path $copilotPath) {
    Remove-Item $copilotPath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .github/copilot-instructions.md"
}

# Remove .github/skills
$githubSkillsDir = Join-Path $ProjectDir ".github" "skills"
if (Test-Path $githubSkillsDir) {
    Remove-Item $githubSkillsDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .github/skills/"
}

# Remove root .mcp.json (generated for @current-mode roles by Prepare-Workspace)
$rootMcpPath = Join-Path $ProjectDir ".mcp.json"
if (Test-Path $rootMcpPath) {
    Remove-Item $rootMcpPath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .mcp.json"
}

# Remove generated worker agent definitions. Filtered by the generator's own
# naming suffix so any hand-authored custom agents alongside them (a user's
# own .claude/agents/*.md or .github/agents/*.agent.md) are left untouched.
$claudeAgentsDir = Join-Path $ProjectDir ".claude" "agents"
if (Test-Path $claudeAgentsDir) {
    $removed = Get-ChildItem -Path $claudeAgentsDir -Filter "*-worker.md" -ErrorAction SilentlyContinue
    if ($removed) {
        $removed | Remove-Item -Force -ErrorAction SilentlyContinue
        Write-Host "  ✓ Removed .claude/agents/*-worker.md"
    }
}

$githubAgentsDir = Join-Path $ProjectDir ".github" "agents"
if (Test-Path $githubAgentsDir) {
    $removed = Get-ChildItem -Path $githubAgentsDir -Filter "*-worker.agent.md" -ErrorAction SilentlyContinue
    if ($removed) {
        $removed | Remove-Item -Force -ErrorAction SilentlyContinue
        Write-Host "  ✓ Removed .github/agents/*-worker.agent.md"
    }
}

# Step 5: Remove state directories
Write-Host ""
Write-Host "Step 5: Removing state directories..."
$dirsToRemove = @(".Kiln")
foreach ($dir in $dirsToRemove) {
    $dirPath = Join-Path $ProjectDir $dir
    if (Test-Path $dirPath) {
        Remove-Item $dirPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  ✓ Removed $dir/"
    }
}

# Remove skill junctions from root .claude/skills
$rootSkillsDir = Join-Path $ProjectDir ".claude" "skills"
if (Test-Path $rootSkillsDir) {
    Remove-Item $rootSkillsDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .claude/skills/"
}

# Step 6: Remove git hooks
Write-Host ""
Write-Host "Step 6: Removing git hooks..."
$hookPath = Join-Path $ProjectDir ".git" "hooks" "pre-push"
if (Test-Path $hookPath) {
    Remove-Item $hookPath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✓ Removed .git/hooks/pre-push"
}

Write-Host ""
Write-Host "Cleanup complete." -ForegroundColor Green
Write-Host "Kiln artifacts have been removed. The project is ready for a fresh Kiln.ps1 run."

