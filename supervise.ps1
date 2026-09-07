<#
.SYNOPSIS
    Keeps the transcode running until it finishes.

.DESCRIPTION
    Restarts the transcode whenever it stops before completing, and waits for
    the machine to have enough free memory before each attempt. A 15+ hour job
    on a workstation will get interrupted: by an out-of-memory kill, a crashed
    parent process, or a reboot. Resume is by verified output file, so
    restarting is always safe and never redoes finished work.

    It will NOT retry forever. If an attempt completes without producing a
    single new file, twice in a row, it stops and says so. An unbounded retry
    loop around a real defect is how one bug becomes a directory full of
    broken files overnight. A run killed while memory was scarce does not count
    toward that limit, since that is the environment failing, not the job.

.PARAMETER Root
    Directory holding the toolkit and library.json. Defaults to this script's
    own directory, so the repo can live anywhere.

.PARAMETER Out
    Output directory for the transcoded library. Defaults to "output" under Root.

.PARAMETER MinFreeGB
    Do not start an attempt below this much free RAM. Two encoders need well
    under a gigabyte; the margin is to avoid being the process that tips an
    already-loaded machine over.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File supervise.ps1

.EXAMPLE
    Get-Content .\output\supervisor.log -Wait     # follow progress
#>
[CmdletBinding()]
param(
    [string]$Root = $PSScriptRoot,
    [string]$Out,
    [double]$MinFreeGB = 2.5,
    [int]$MaxAttempts = 60,
    [int]$NoProgressLimit = 2
)

$ErrorActionPreference = 'Stop'

if (-not $Out) { $Out = Join-Path $Root 'output' }
$SupLog   = Join-Path $Out 'supervisor.log'
$LockFile = Join-Path $Out 'supervisor.RUNNING'

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $SupLog -Value $line -Encoding utf8
}

function Get-DoneCount {
    @(Get-ChildItem -Path $Out -Filter '*.mp4' -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike '*.part.mp4' }).Count
}

function Get-FreeGB {
    (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
}

if (-not (Test-Path $Out)) { New-Item -ItemType Directory -Path $Out -Force | Out-Null }

# Single instance. A stale lock left by a hard kill is reclaimed, since the PID
# it names will no longer exist.
if (Test-Path $LockFile) {
    $oldPid = Get-Content $LockFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Log "supervisor already running as pid $oldPid; exiting"
        exit 0
    }
    Write-Log "clearing stale lock from pid $oldPid"
    Remove-Item $LockFile -Force
}
Set-Content -Path $LockFile -Value $PID -Encoding ascii

try {
    Write-Log "=== supervisor started (pid $PID) ==="
    $noProgress = 0

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {

        $done = Get-DoneCount

        # Wait until the machine can take it.
        $waited = 0
        while ((Get-FreeGB) -lt $MinFreeGB) {
            if ($waited % 300 -eq 0) {
                Write-Log ("waiting for memory: {0:N1} GB free, need {1} GB" -f (Get-FreeGB), $MinFreeGB)
            }
            Start-Sleep -Seconds 30
            $waited += 30
        }

        $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
        $outLog = Join-Path $Out "run-$stamp.log"
        $errLog = Join-Path $Out "run-$stamp.err.log"
        Write-Log ("attempt {0}: starting at {1} done, {2:N1} GB free -> run-{3}.log" -f `
                   $attempt, $done, (Get-FreeGB), $stamp)

        $proc = Start-Process -FilePath 'python' `
            -ArgumentList 'transcode.py' `
            -WorkingDirectory $Root -WindowStyle Hidden `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
        $proc.WaitForExit()
        $code = $proc.ExitCode

        $after = Get-DoneCount
        $made  = $after - $done
        Write-Log "attempt $attempt ended: exit $code, $made new file(s), $after total"

        if ($code -eq 0) {
            # Thorough verification runs only here, with nothing else touching
            # the disk. It reports and never deletes, so anything it finds needs
            # a human rather than an automatic retry.
            Write-Log "transcode complete; verifying"
            & python (Join-Path $Root 'transcode.py') --verify 2>&1 |
                ForEach-Object { Write-Log "  $_" }
            if ($LASTEXITCODE -ne 0) {
                Write-Log "VERIFY REPORTED PROBLEMS (exit $LASTEXITCODE). Nothing was deleted."
            } else {
                Write-Log "verify clean"
            }
            Write-Log "building index"
            & python (Join-Path $Root 'make_index.py') 2>&1 | ForEach-Object { Write-Log "  $_" }
            Write-Log "=== COMPLETE ==="
            break
        }

        $freeNow = Get-FreeGB
        if ($made -gt 0) {
            $noProgress = 0
        } elseif ($freeNow -lt $MinFreeGB) {
            # Killed by memory pressure, not by a defect. Counting this against
            # the give-up limit would make the supervisor quit permanently the
            # moment the machine got busy, which is the opposite of its purpose.
            Write-Log ("no progress, but only {0:N1} GB free: memory pressure, not a defect" -f $freeNow)
        } else {
            $noProgress++
            Write-Log "no progress on this attempt ($noProgress/$NoProgressLimit)"
            if ($noProgress -ge $NoProgressLimit) {
                Write-Log "STOPPING: $NoProgressLimit consecutive attempts made no progress."
                Write-Log "See the newest run-*.log and run-*.err.log."
                break
            }
        }

        $backoff = [Math]::Min(300, 30 * $noProgress + 30)
        Write-Log "restarting in ${backoff}s"
        Start-Sleep -Seconds $backoff
    }
}
finally {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    Write-Log "=== supervisor exiting (pid $PID) ==="
}
