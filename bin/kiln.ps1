#!/usr/bin/env pwsh
# Kiln entry point (Windows) — a shim.
#
# All logic lives in Python under src/kiln/launcher/. This script only locates the
# framework, puts it on PYTHONPATH, and forwards its arguments through unchanged. The
# launcher accepts both spellings, so existing invocations keep working:
#
#   .\bin\kiln.ps1 -WorkingDir C:\path\to\project
#   .\bin\kiln.ps1 -Init -WorkingDir C:\path\to\project -Example library-hub
#   .\bin\kiln.ps1 init C:\path\to\project
#   .\bin\kiln.ps1 -Stop
#   .\bin\kiln.ps1 -ListProfiles
#
# The previous ~1800-line PowerShell implementation was replaced by the Python launcher so
# Windows and Unix share one implementation instead of two that drift apart. See
# full-python-plan.md.

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$frameworkRoot = Split-Path -Parent $scriptDir
$packageRoot = Join-Path -Path $frameworkRoot -ChildPath "src"

if (-not (Test-Path (Join-Path $packageRoot "kiln\launcher"))) {
    Write-Host "Error: Kiln launcher package not found at $packageRoot" -ForegroundColor Red
    exit 1
}

# Prefer a project-local virtualenv when present, otherwise whatever `python` resolves to.
# The launcher and scheduler import only the standard library, so no install step is needed.
$venvPython = Join-Path -Path $frameworkRoot -ChildPath (Join-Path -Path ".venv" -ChildPath (Join-Path -Path "Scripts" -ChildPath "python.exe"))
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

if ($python -eq "python" -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Error: 'python' is required but was not found on PATH." -ForegroundColor Red
    exit 1
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$packageRoot;$env:PYTHONPATH" } else { $packageRoot }

& $python -m kiln.launcher.infrastructure.cli @args
exit $LASTEXITCODE
