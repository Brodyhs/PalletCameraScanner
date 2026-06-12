<#
.SYNOPSIS
Install the PalletScan station service: a Task Scheduler task that starts
`palletscan supervise` at logon of the station user, and restarts the
*supervisor* if it ever dies. Child (pipeline) restarts are the
supervisor's job — in ~5 s with countable exit codes — because Task
Scheduler's own restart has a 1-minute floor and records only the last
run result.

.DESCRIPTION
Run from an elevated PowerShell in the repo's deploy\ directory:

  .\install_service.ps1 -RepoDir C:\palletscan `
      -ConfigPath C:\palletscan\config\station.yaml `
      -DataDir C:\palletscan\data

The task runs as the *interactive station user* (not SYSTEM): UVC camera
capture via the Windows camera frame server is gated per-user and is a
known failure mode under session 0. Configure OS auto-logon with netplwiz
(RUNBOOK.md §5) so the session exists after reboot.
#>
param(
    [string]$TaskName = "PalletScan",
    [string]$RepoDir = "C:\palletscan",
    [string]$VenvPython = "C:\palletscan\.venv\Scripts\python.exe",
    [string]$ConfigPath = "C:\palletscan\config\station.yaml",
    [string]$DataDir = "C:\palletscan\data",
    [string]$User = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VenvPython)) {
    throw "venv python not found at $VenvPython (RUNBOOK.md section 2: install first)"
}

$arguments = "-m palletscan supervise --data-dir `"$DataDir`" -- run --config `"$ConfigPath`""

$action = New-ScheduledTaskAction -Execute $VenvPython -Argument $arguments `
    -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan)   # zero = never time the task out
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' (runs as $User at logon)."
Write-Host "Start it now:    .\start_palletscan.ps1   (derives -DataDir from the task)"
Write-Host "Stop (verified): .\stop_palletscan.ps1    (derives -DataDir from the task)"
