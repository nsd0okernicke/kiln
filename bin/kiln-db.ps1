#!/usr/bin/env pwsh
<#
.SYNOPSIS
Kiln message database CLI tool.
Inspect, query, and manage the message database using sqlite3.

.EXAMPLE
.\Kiln-db.ps1 list-messages coder
.\Kiln-db.ps1 show-message msg-20260611114532xxxx-xxxx
.\Kiln-db.ps1 stats
.\Kiln-db.ps1 clear-old -Before "-7 days"
.\Kiln-db.ps1 retry-message msg-20260611114532xxxx-xxxx
#>

param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet('list-messages', 'show-message', 'stats', 'clear-old', 'retry-message')]
    [string]$Command,

    [Parameter(Position=1)]
    [string]$Argument = "",

    [string]$Before = "-7 days",
    [string]$Status = "queued"
)

$ErrorActionPreference = "Stop"

# Find project root
function Find-ProjectRoot {
    $searchDir = (Get-Location).Path
    $maxDepth = 20
    $depth = 0

    while ($searchDir -ne [System.IO.Path]::GetPathRoot($searchDir) -and $depth -lt $maxDepth) {
        if (Test-Path (Join-Path $searchDir ".Kiln")) {
            return $searchDir
        }
        $searchDir = Split-Path -Parent $searchDir
        $depth++
    }

    Write-Host "Error: Could not find Kiln project root" -ForegroundColor Red
    exit 1
}

# Run SQLite query and return results
function Invoke-SqlQuery {
    param(
        [string]$DbPath,
        [string]$Query,
        [switch]$Json = $false
    )

    if (-not (Test-Path $DbPath)) {
        Write-Host "Error: Database not found at: $DbPath" -ForegroundColor Red
        exit 1
    }

    $cmdArgs = @($DbPath)
    if ($Json) {
        $cmdArgs += ".mode json"
    } else {
        $cmdArgs += ".mode line"
    }
    $cmdArgs += $Query

    try {
        $result = sqlite3 @cmdArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "SQLite error: $result" -ForegroundColor Red
            exit 1
        }
        return $result
    } catch {
        Write-Host "Error: sqlite3 command not found. Please install SQLite." -ForegroundColor Red
        exit 1
    }
}

# ============================================================================
# Main
# ============================================================================

$ProjectRoot = Find-ProjectRoot
$DbPath = Join-Path $ProjectRoot ".Kiln" "messages.db"

switch ($Command) {
    'list-messages' {
        if ([string]::IsNullOrEmpty($Argument)) {
            Write-Host "Usage: Kiln-db.ps1 list-messages <role> [-Status <status>]" -ForegroundColor Yellow
            Write-Host "  Statuses: queued, delivered, acked, processed, failed" -ForegroundColor Gray
            exit 1
        }

        $query = @"
SELECT
  id,
  sender,
  priority,
  status,
  created_at,
  delivered_at,
  processed_at,
  error
FROM messages
WHERE target = '$Argument' AND status = '$Status'
ORDER BY priority ASC, created_at ASC;
"@

        $result = Invoke-SqlQuery -DbPath $DbPath -Query $query -Json

        if ([string]::IsNullOrEmpty($result)) {
            Write-Host "No messages found for role '$Argument' with status '$Status'" -ForegroundColor Cyan
            exit 0
        }

        Write-Host "Messages for '$Argument' ($Status):" -ForegroundColor Cyan
        Write-Host ""

        $messages = $result | ConvertFrom-Json
        if ($messages -is [object[]]) {
            $count = $messages.Count
        } else {
            $messages = @($messages)
            $count = 1
        }

        foreach ($msg in $messages) {
            $idShort = if ($msg.id.Length -gt 20) { $msg.id.Substring(0, 20) + "..." } else { $msg.id }
            Write-Host "ID: $idShort" -ForegroundColor Green
            Write-Host "  From: $($msg.sender)"
            Write-Host "  Priority: $($msg.priority)"
            Write-Host "  Created: $($msg.created_at)"
            Write-Host "  Status: $($msg.status)"
            if ($msg.delivered_at) { Write-Host "  Delivered: $($msg.delivered_at)" -ForegroundColor Cyan }
            if ($msg.processed_at) { Write-Host "  Processed: $($msg.processed_at)" -ForegroundColor Green }
            if ($msg.error) { Write-Host "  Error: $($msg.error)" -ForegroundColor Red }
            Write-Host ""
        }
        Write-Host "Total: $count message(s)" -ForegroundColor Cyan
    }

    'show-message' {
        if ([string]::IsNullOrEmpty($Argument)) {
            Write-Host "Usage: Kiln-db.ps1 show-message <message-id>" -ForegroundColor Yellow
            exit 1
        }

        $query = @"
SELECT * FROM messages WHERE id LIKE '$Argument%' LIMIT 1;
"@

        $result = Invoke-SqlQuery -DbPath $DbPath -Query $query

        if ([string]::IsNullOrEmpty($result)) {
            Write-Host "Message not found: $Argument" -ForegroundColor Red
            exit 1
        }

        Write-Host "Message Details:" -ForegroundColor Green
        Write-Host ""
        Write-Host $result
        Write-Host ""
    }

    'stats' {
        $query = "SELECT status, COUNT(*) as count FROM messages GROUP BY status ORDER BY count DESC;"

        $result = Invoke-SqlQuery -DbPath $DbPath -Query $query

        Write-Host "Message Database Statistics:" -ForegroundColor Cyan
        Write-Host ""
        Write-Host $result
        Write-Host ""

        $totalQuery = "SELECT COUNT(*) as total FROM messages;"
        $totalResult = Invoke-SqlQuery -DbPath $DbPath -Query $totalQuery
        Write-Host "Total messages: $totalResult" -ForegroundColor Cyan
    }

    'clear-old' {
        $dayOffset = [int]($Before.Split()[0])
        $beforeDate = (Get-Date).AddDays($dayOffset).ToString("yyyy-MM-dd HH:mm:ss")

        $dryRunQuery = @"
SELECT COUNT(*) as count FROM messages
WHERE status = 'processed' AND created_at < '$beforeDate';
"@

        $dryCount = Invoke-SqlQuery -DbPath $DbPath -Query $dryRunQuery

        Write-Host "Dry run: would delete $dryCount old processed message(s) created before $beforeDate" -ForegroundColor Yellow

        $response = Read-Host "Delete? (y/n)"
        if ($response -eq "y" -or $response -eq "yes") {
            $deleteQuery = @"
DELETE FROM messages
WHERE status = 'processed' AND created_at < '$beforeDate';
"@
            Invoke-SqlQuery -DbPath $DbPath -Query $deleteQuery | Out-Null
            Write-Host "Deleted $dryCount message(s)" -ForegroundColor Green
        } else {
            Write-Host "Cancelled." -ForegroundColor Cyan
        }
    }

    'retry-message' {
        if ([string]::IsNullOrEmpty($Argument)) {
            Write-Host "Usage: Kiln-db.ps1 retry-message <message-id>" -ForegroundColor Yellow
            exit 1
        }

        $checkQuery = "SELECT status, target FROM messages WHERE id LIKE '$Argument%' LIMIT 1;"
        $checkResult = Invoke-SqlQuery -DbPath $DbPath -Query $checkQuery

        if ([string]::IsNullOrEmpty($checkResult)) {
            Write-Host "Message not found: $Argument" -ForegroundColor Red
            exit 1
        }

        if ($checkResult -like "*queued*") {
            Write-Host "Message is already queued." -ForegroundColor Yellow
            exit 0
        }

        $updateQuery = @"
UPDATE messages SET status = 'queued', delivered_at = NULL
WHERE id LIKE '$Argument%';
"@

        Invoke-SqlQuery -DbPath $DbPath -Query $updateQuery | Out-Null
        Write-Host "Message moved back to 'queued' status" -ForegroundColor Green
        Write-Host "  ID: $Argument" -ForegroundColor Cyan
    }

    default {
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        exit 1
    }
}

