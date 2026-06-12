<#
.SYNOPSIS
Remove the PalletScan scheduled task. Stops the supervisor gracefully
first (stop-file), then unregisters the task. Data, logs and evidence are
left in place.
#>
param(
    [string]$TaskName = "PalletScan",
    [string]$DataDir = "C:\palletscan\data"
)

$ErrorActionPreference = "Stop"

& "$PSScriptRoot\stop_palletscan.ps1" -TaskName $TaskName -DataDir $DataDir
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'. Data under $DataDir is untouched."
