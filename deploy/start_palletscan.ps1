<#
.SYNOPSIS
Start the PalletScan scheduled task now (it also starts by itself at the
station user's logon).
#>
param([string]$TaskName = "PalletScan")

$ErrorActionPreference = "Stop"
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started '$TaskName'. Dashboard (if enabled): http://127.0.0.1:8000"
