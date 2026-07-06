#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Create a new Kiln project with full configuration and git initialization.
.DESCRIPTION
    Scaffolds a new Kiln project by copying constitution, roles, and tools from
    the framework, then initializes git. Optionally includes an example project brief.
.PARAMETER Target
    Path where the Kiln project will be initialized. Can be a new or existing directory.
.PARAMETER Example
    Optional example to copy: 'library-hub' includes the example README.md as project brief.
.PARAMETER NoGit
    If specified, skip git initialization (useful if the directory is already a git repo).
.EXAMPLE
    .\new-project.ps1 -Target C:\projects\my-app
    .\new-project.ps1 -Target C:\projects\existing-project -NoGit
    .\new-project.ps1 -Target C:\projects\library-hub -Example library-hub
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$Target,

    [Parameter(Mandatory=$false)]
    [string]$Example = "",

    [Parameter(Mandatory=$false)]
    [string]$Profile = "dev",

    [Parameter(Mandatory=$false)]
    [switch]$NoGit = $false,

    [Parameter(Mandatory=$false)]
    [switch]$ListProfiles = $false
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$FrameworkRoot = Split-Path -Parent $ScriptDir

if (-not (Test-Path "$FrameworkRoot\kiln")) {
    Write-Host "Error: Could not find kiln framework directory." -ForegroundColor Red
    Write-Host "This script must be run from bin/kiln-init.ps1 (in the Kiln repository)." -ForegroundColor Yellow
    exit 1
}

# Handle -ListProfiles flag
if ($ListProfiles) {
    Write-Host "Available Kiln configuration profiles:" -ForegroundColor Green
    Write-Host ""
    $profilesPath = Join-Path $FrameworkRoot "kiln" "profiles.json"
    $config = Get-Content -Path $profilesPath -Raw | ConvertFrom-Json
    foreach ($profileProp in $config.profiles.PSObject.Properties) {
        $desc = $profileProp.Value.description
        Write-Host "  $($profileProp.Name)`t$desc" -ForegroundColor Cyan
    }
    exit 0
}

if (Test-Path $Target) {
    Write-Host "Initializing Kiln in existing directory: $Target" -ForegroundColor Green
} else {
    Write-Host "Creating Kiln project: $Target" -ForegroundColor Green
}

New-Item -ItemType Directory -Path $Target -Force | Out-Null

function Write-ClaudeCodeConfig {
    param([string]$ProjectPath)
    $claudeDir = Join-Path $ProjectPath ".claude"
    New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
    Set-Content -Path (Join-Path $claudeDir ".gitignore") -Value "*" -Encoding UTF8
    $templateSettings = Join-Path $FrameworkRoot "kiln" ".claude" "settings.json"
    $targetSettings = Join-Path $claudeDir "settings.json"
    if (Test-Path $templateSettings) {
        Copy-Item -Path $templateSettings -Destination $targetSettings -Force
    }
}

$KilnDir = Join-Path $Target "kiln"
$constitutionDir = Join-Path $KilnDir "constitution"
$rolesDir = Join-Path $KilnDir "roles"
$skillsDir = Join-Path $KilnDir "skills"
$KilnInfraDir = Join-Path $Target ".kiln"

try {
    New-Item -ItemType Directory -Path $constitutionDir -Force | Out-Null
    New-Item -ItemType Directory -Path $rolesDir -Force | Out-Null
    New-Item -ItemType Directory -Path $skillsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $KilnInfraDir -Force | Out-Null
    Write-Host "✓ Created directory structure" -ForegroundColor Green
}
catch {
    Write-Host "Error creating directories: $_" -ForegroundColor Red
    exit 1
}


$frameworkConstitution = Join-Path $FrameworkRoot "kiln\constitution"
@("engineering.md", "workflow.md", "project.md") | ForEach-Object {
    $source = Join-Path $frameworkConstitution $_
    $dest = Join-Path $constitutionDir $_
    if (Test-Path $source) {
        Copy-Item -Path $source -Destination $dest -Force
    }
}
Write-Host "✓ Copied constitution files" -ForegroundColor Green

$frameworkRoles = Join-Path $FrameworkRoot "kiln\roles"
if (Test-Path $frameworkRoles) {
    Get-ChildItem -Path $frameworkRoles -Filter "*.md" | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination $rolesDir -Force
    }
    Write-Host "✓ Copied role files" -ForegroundColor Green
}

# Note: Profiles are not copied to the target project.
# Projects inherit framework profiles; override by creating kiln.profiles.json at project root if needed.

$frameworkSkills = Join-Path $FrameworkRoot "kiln\skills"
if (Test-Path $frameworkSkills) {
    Get-ChildItem -Path $frameworkSkills -Directory | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination $skillsDir -Recurse -Force
    }
    Write-Host "✓ Copied skills" -ForegroundColor Green
}



$constitutionMdPath = Join-Path $KilnDir "constitution.md"
$constitutionMdContent = @'
# Kiln Constitution

This file takes precedence over subordinate files.
Read and obey the following subordinate documents in order.

1. kiln/constitution/project.md
2. kiln/constitution/engineering.md
3. kiln/constitution/workflow.md

If two subordinate files conflict, the earlier file wins.
'@
Set-Content -Path $constitutionMdPath -Value $constitutionMdContent -Encoding UTF8
Write-Host "✓ Created constitution.md" -ForegroundColor Green


Write-ClaudeCodeConfig -ProjectPath $Target
Write-Host "✓ Created .claude/settings.json" -ForegroundColor Green

# Create .mcp.json in project root with MCP server configuration (for Copilot agents)
$dbPath = Join-Path $Target ".kiln" "messages.db"
$dbPathEscaped = $dbPath -replace '\\', '\\'
$mcpJsonPath = Join-Path $Target ".mcp.json"
$mcpJson = @"
{
  "mcpServers": {
    "kiln-db": {
      "command": "npx",
      "args": ["mcp-sqlite", "$dbPathEscaped"]
    }
  }
}
"@
Set-Content -Path $mcpJsonPath -Value $mcpJson -Encoding UTF8
Write-Host "✓ Created .mcp.json (MCP server configuration)" -ForegroundColor Green

if ($Example -eq "library-hub") {
    $exampleReadme = Join-Path $FrameworkRoot "examples\library-hub\README.md"
    if (Test-Path $exampleReadme) {
        Copy-Item -Path $exampleReadme -Destination (Join-Path $Target "README.md") -Force
        Write-Host "✓ Copied example README.md" -ForegroundColor Green
    }
    $exampleProjectMd = Join-Path $FrameworkRoot "examples\$Example\kiln\constitution\project.md"
    if (Test-Path $exampleProjectMd) {
        Copy-Item -Path $exampleProjectMd -Destination (Join-Path $constitutionDir "project.md") -Force
        Write-Host "✓ Copied example-specific project.md" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "Initializing database..." -ForegroundColor Cyan
$dbPath = Join-Path $Target ".kiln" "messages.db"
$pythonScript = @"
import sqlite3, os
db = r'$dbPath'
conn = sqlite3.connect(db)
conn.execute('''CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), sender TEXT NOT NULL, target TEXT NOT NULL,
  priority INTEGER DEFAULT 50, status TEXT DEFAULT 'queued',
  content TEXT NOT NULL, created_at TEXT NOT NULL,
  delivered_at TEXT, acked_at TEXT, processed_at TEXT, error TEXT,
  branch TEXT NOT NULL DEFAULT 'main')''')
conn.execute('CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status)')
conn.execute('PRAGMA journal_mode=WAL')
conn.commit()
conn.close()
"@
python -c $pythonScript 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "✓ Initialized message database" -ForegroundColor Green
} else {
    Write-Host "Warning: Failed to initialize database via Python, trying direct sqlite3..." -ForegroundColor Yellow
    $sqliteScript = @"
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), sender TEXT NOT NULL, target TEXT NOT NULL,
  priority INTEGER DEFAULT 50, status TEXT DEFAULT 'queued',
  content TEXT NOT NULL, created_at TEXT NOT NULL,
  delivered_at TEXT, acked_at TEXT, processed_at TEXT, error TEXT,
  branch TEXT NOT NULL DEFAULT 'main');
CREATE INDEX IF NOT EXISTS idx_target_branch_status ON messages(target,branch,status);
PRAGMA journal_mode=WAL;
"@
    $sqliteScript | sqlite3 $dbPath
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ Initialized message database via sqlite3" -ForegroundColor Green
    } else {
        Write-Host "Warning: Could not initialize database (sqlite3 not found)" -ForegroundColor Yellow
        Write-Host "Kiln messaging requires either Python or sqlite3 to be installed." -ForegroundColor Yellow
    }
}

if (-not $NoGit) {
    Write-Host ""
    Write-Host "Initializing git repository..." -ForegroundColor Cyan

    try {
        git --version | Out-Null
    }
    catch {
        Write-Host "Error: Git is not installed or not in PATH." -ForegroundColor Red
        exit 1
    }

    try {
        $isGitRepo = git -C $Target rev-parse --git-dir 2>$null
        $gitInitialized = $?

        if (-not $gitInitialized) {
            git -C $Target init | Out-Null
            git -C $Target branch -M main 2>$null | Out-Null
            Write-Host "✓ Initialized new git repository on branch 'main'" -ForegroundColor Green
        } else {
            Write-Host "✓ Using existing git repository" -ForegroundColor Green
        }

        $gitignorePath = Join-Path $Target ".gitignore"
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
.kiln/
.worktrees/
.github/
.claude/skills
'@
        Set-Content -Path $gitignorePath -Value $gitignoreContent -Encoding UTF8
        Write-Host "✓ Created .gitignore" -ForegroundColor Green
    }
    catch {
        Write-Host "Error during git initialization: $_" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host ""
    Write-Host "Skipping git initialization (use -NoGit for existing repos)" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "✓ Project created successfully: $Target" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. cd $Target"
Write-Host "  2. Review kiln/constitution/ and kiln/roles/"
Write-Host "  3. git add kiln/ .claude/ && git commit -m 'Add Kiln configuration'"
Write-Host "  4. Run: kiln.ps1 to launch agents"
Write-Host ""

