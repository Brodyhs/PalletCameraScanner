<#
.SYNOPSIS
Start the PalletScan scheduled task (it also starts by itself at the
station user's logon) and verify it actually came up.

.DESCRIPTION
An explicit start re-arms a stopped station: the stop-file
(<DataDir>\supervisor.stop) is a sticky latch that any starting supervisor
honors (exit 0, no child), so this script removes it FIRST — that removal
is the one and only way a stop request is ever discarded, and it is an
explicit operator action (REVIEW_SYSTEM_0c30c77 findings 13/15).

DataDir is derived from the registered task's --data-dir argument when not
passed, so a bare invocation can never clear the latch in the wrong
directory and then "start" a station that immediately honors the real one.
#>
param(
    [string]$TaskName = "PalletScan",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

function Get-StationDataDir {
    param([string]$TaskName, [string]$Explicit)
    if ($Explicit) { return $Explicit }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $arguments = ($task.Actions | ForEach-Object { $_.Arguments }) -join " "
    if ($arguments -match '--data-dir[= ]+"([^"]+)"') { return $Matches[1] }
    if ($arguments -match '--data-dir[= ]+(\S+)') { return $Matches[1] }
    throw "could not derive the data dir from task '$TaskName'; pass -DataDir"
}

$DataDir = Get-StationDataDir -TaskName $TaskName -Explicit $DataDir
$stopFile = Join-Path $DataDir "supervisor.stop"
if (Test-Path $stopFile) {
    Remove-Item $stopFile -Force
    Write-Host "Removed stop latch $stopFile (explicit start re-arms the station)."
}

Start-ScheduledTask -TaskName $TaskName

# Verify the start instead of assuming it: a lingering latch elsewhere or a
# task misconfiguration would otherwise print success over a dead station.
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    if ($state -eq "Running") {
        Write-Host "Started '$TaskName' (task state: Running). Dashboard (if enabled): http://127.0.0.1:8000"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Write-Error ("Task '$TaskName' did not reach the Running state within 15 s " +
    "(state: $((Get-ScheduledTask -TaskName $TaskName).State)). Check " +
    "$DataDir\logs\supervisor.jsonl and restarts.jsonl.")
exit 1
