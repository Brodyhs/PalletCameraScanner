<#
.SYNOPSIS
Stop PalletScan and VERIFY it is dead — never report "stopped" while
anything still scans.

.DESCRIPTION
Protocol (REVIEW_SYSTEM_0c30c77 findings 6/7/13/15):

1. The stop request is <DataDir>\supervisor.stop — a STICKY LATCH. The
   supervisor stops its child when it appears, and any supervisor starting
   while it exists honors it (exits without spawning). This script never
   removes it; start_palletscan.ps1 does, on an explicit start.
2. "Stopped" is verified through the instance locks, not assumed from
   stop-file consumption: the OS releases palletscan.lock /
   palletscan.supervisor.lock when their holders die — cleanly or not — so
   a NON-BLOCKING LOCK ATTEMPT on the lock byte (offset 0x100000, mirrors
   palletscan/reliability/instance_lock.py) is a death-proof liveness
   probe, including for orphans this script knows nothing about. A probe
   conflict = a live holder.
3. The hard path stops the scheduled task (the supervisor's kill-on-close
   job object takes the pipeline child with it — best-effort) AND
   explicitly kills the writer-lock holder's pid (read from the lock
   file's diagnostics JSON), covering pre-existing orphans and
   job-assignment failures. Lock release after a kill can lag (MSDN:
   indeterminate after abnormal close), so verification polls ~10 s.

DataDir is derived from the registered task's --data-dir argument when not
passed, so a bare invocation can never act on the wrong directory (the old
script's silent default-dir mismatch made every graceful stop escalate).
#>
param(
    [string]$TaskName = "PalletScan",
    [string]$DataDir = "",
    [int]$TimeoutSeconds = 30,
    [switch]$Hard
)

$ErrorActionPreference = "Stop"

# Must match LOCK_BYTE_OFFSET in palletscan/reliability/instance_lock.py.
$LockByteOffset = 0x100000

function Get-StationDataDir {
    param([string]$TaskName, [string]$Explicit)
    if ($Explicit) { return $Explicit }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $arguments = ($task.Actions | ForEach-Object { $_.Arguments }) -join " "
    if ($arguments -match '--data-dir[= ]+"([^"]+)"') { return $Matches[1] }
    if ($arguments -match '--data-dir[= ]+(\S+)') { return $Matches[1] }
    throw "could not derive the data dir from task '$TaskName'; pass -DataDir"
}

function Test-LockHeld {
    # $true = a live process holds the lock; $false = free; on any open
    # failure we report held (refuse to claim "stopped" on uncertainty).
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $false }   # never ran: free
    try {
        $fs = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::ReadWrite
        )
    } catch {
        Write-Warning "cannot open $Path to probe ($_); treating as RUNNING"
        return $true
    }
    try {
        # Lock-vs-lock conflict is the exact semantic the holder uses
        # (locking past EOF is legal); a read probe past EOF is not
        # specified. Worst case the transient probe-lock costs a starting
        # child one exit-4 backoff cycle, which is self-healing.
        $fs.Lock($LockByteOffset, 1)
        $fs.Unlock($LockByteOffset, 1)
        return $false
    } catch [System.IO.IOException] {
        return $true
    } finally {
        $fs.Dispose()
    }
}

function Get-LockHolderPid {
    # Best-effort: the holder writes {pid, started, argv} JSON at offset 0
    # (readable; the lock byte sits at 1 MiB precisely so this works).
    param([string]$Path)
    try {
        $line = Get-Content -Path $Path -TotalCount 1 -ErrorAction Stop
        return ($line | ConvertFrom-Json).pid
    } catch {
        return $null
    }
}

function Wait-LocksFree {
    param([string]$WriterLock, [string]$SupervisorLock, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-LockHeld $WriterLock) -and -not (Test-LockHeld $SupervisorLock)) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return (-not (Test-LockHeld $WriterLock)) -and (-not (Test-LockHeld $SupervisorLock))
}

$DataDir = Get-StationDataDir -TaskName $TaskName -Explicit $DataDir
$writerLock = Join-Path $DataDir "palletscan.lock"
$supervisorLock = Join-Path $DataDir "palletscan.supervisor.lock"
$stopFile = Join-Path $DataDir "supervisor.stop"

# Refuse to act on a directory that has never hosted a station: writing a
# stop-file into the wrong dir silently does nothing while this script
# reports progress (the old default-dir trap).
if (-not (Test-Path $writerLock) -and -not (Test-Path $supervisorLock) `
        -and -not (Test-Path (Join-Path $DataDir "logs"))) {
    Write-Error ("no PalletScan station state found under '$DataDir'; " +
        "pass -DataDir matching the installed -DataDir")
    exit 2
}

if (-not (Test-LockHeld $writerLock) -and -not (Test-LockHeld $supervisorLock)) {
    New-Item -ItemType File -Path $stopFile -Force | Out-Null   # latch anyway
    Write-Host ("PalletScan is not running (instance locks free); stop " +
        "latch written so it stays down until start_palletscan.ps1.")
    exit 0
}

if (-not $Hard) {
    New-Item -ItemType File -Path $stopFile -Force | Out-Null
    Write-Host "Wrote $stopFile; waiting for the station to stop (verified via instance locks) ..."
    if (Wait-LocksFree $writerLock $supervisorLock $TimeoutSeconds) {
        Write-Host "PalletScan stopped (verified: instance locks released). The stop latch keeps it down until start_palletscan.ps1."
        exit 0
    }
    Write-Warning ("Station still running after $TimeoutSeconds s " +
        "(wedged supervisor, or an orphaned writer). Escalating to a hard stop.")
}

# Hard path: stop the task (kills the supervisor; its kill-on-close job
# object takes the child — best-effort), then explicitly kill the
# writer-lock holder (covers orphans and job-assignment failures).
New-Item -ItemType File -Path $stopFile -Force | Out-Null   # keep the latch
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {
    Write-Warning "Stop-ScheduledTask failed: $_"
}
foreach ($lock in @($writerLock, $supervisorLock)) {
    if (Test-LockHeld $lock) {
        $holderPid = Get-LockHolderPid $lock
        if ($holderPid) {
            Write-Host "Killing lock holder pid $holderPid ($lock) ..."
            try { Stop-Process -Id $holderPid -Force -ErrorAction Stop } catch {
                Write-Warning "Stop-Process ${holderPid}: $_"
            }
        } else {
            Write-Warning "$lock is held but its holder pid is unreadable"
        }
    }
}
# Lock release after a kill is not instantaneous; poll before judging.
if (Wait-LocksFree $writerLock $supervisorLock 10) {
    Write-Host ("PalletScan hard-stopped (verified: instance locks released). " +
        "Durable state is safe; in-flight queue contents were dropped. " +
        "The stop latch keeps it down until start_palletscan.ps1.")
    exit 0
}
Write-Error ("PalletScan is STILL RUNNING: an instance lock is held after the " +
    "hard stop. Inspect the holder (first JSON line of $writerLock / " +
    "$supervisorLock) and kill it manually. Do NOT archive or edit the data dir.")
exit 1
