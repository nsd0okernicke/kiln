#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Clear all messages from the Kiln message database.

.DESCRIPTION
    Deletes all entries from the messages table in .Kiln/messages.db.
    Use this to reset the communication queue for testing.

.PARAMETER ProjectRoot
    Path to the project root. Defaults to current directory.

.EXAMPLE
    .\clear-messages.ps1
    .\clear-messages.ps1 -ProjectRoot C:\projects\my-project
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = "Stop"

# Find project root if not provided
if (-not (Test-Path (Join-Path $ProjectRoot ".Kiln" "messages.db"))) {
    Write-Host "Error: No messages.db found at $ProjectRoot/.Kiln/messages.db" -ForegroundColor Red
    exit 1
}

$dbPath = Join-Path $ProjectRoot ".Kiln" "messages.db"

Write-Host "Clearing all messages from: $dbPath" -ForegroundColor Yellow
Write-Host ""

$tmp = Join-Path $env:TEMP "clear_$([guid]::NewGuid().ToString().Substring(0,8)).py"
$code = @"
import sqlite3
conn = sqlite3.connect(r'$dbPath')
cursor = conn.cursor()

# Get count before
cursor.execute('SELECT COUNT(*) FROM messages')
before = cursor.fetchone()[0]
print(f'Messages before: {before}')

# Delete all
cursor.execute('DELETE FROM messages')
conn.commit()

# Get count after
cursor.execute('SELECT COUNT(*) FROM messages')
after = cursor.fetchone()[0]
print(f'Messages after: {after}')

conn.close()
"@

Set-Content -Path $tmp -Value $code -Encoding UTF8
$output = python $tmp 2>&1
Write-Host $output
Remove-Item $tmp -Force -EA SilentlyContinue

Write-Host ""
Write-Host "✓ Database cleared" -ForegroundColor Green

