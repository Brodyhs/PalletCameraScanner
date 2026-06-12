<#
.SYNOPSIS
Stop PalletScan gracefully via the supervisor's stop-file.

.DESCRIPTION
Writes <DataDir>\supervisor.stop. The supervisor polls for it (0.5 s),
sends CTRL_BREAK to the pipeline child, waits its --grace-s (15 s default)
for queues to drain, removes the file, and exits 0. This script waits for
the file to be consumed as confirmation.

Why a file and not a signal: console-ctrl events cannot cross Windows
sessions — an operator's PowerShell cannot signal the hidden-console
supervisor — and Stop-ScheduledTask hard-terminates the whole tree.

-Hard skips the grace and hard-stops the task immediately. The crash-only
design tolerates it (SQLite/outbox are durable, evidence pruning is
race-tolerant), but in-flight queue contents are dropped — prefer the
graceful path.
#>
param(
    [string]$TaskName = "PalletScan",
    [string]$DataDir = "C:\palletscan\data",
    [int]$TimeoutSeconds = 30,
    [switch]$Hard
)

$ErrorActionPreference = "Stop"

if ($Hard) {
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host "Hard-stopped '$TaskName' (durable state is safe; in-flight queue contents were dropped)."
    exit 0
}

$stopFile = Join-Path $DataDir "supervisor.stop"
New-Item -ItemType File -Path $stopFile -Force | Out-Null
Write-Host "Wrote $stopFile; waiting for the supervisor to consume it ..."

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Test-Path $stopFile) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Milliseconds 500
}

if (Test-Path $stopFile) {
    Write-Warning ("Supervisor did not consume the stop-file within " +
        "$TimeoutSeconds s (not running, or wedged). Falling back to a hard stop.")
    Remove-Item $stopFile -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $TaskName
} else {
    Write-Host "PalletScan stopped gracefully."
}
