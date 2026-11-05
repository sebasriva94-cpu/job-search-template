# Pure helpers for pull-jobs.ps1, dot-sourced by it and by tests\pull-jobs.lib.Tests.ps1 (Pester 3).
# Nothing here runs a step or touches the network; the functions take the status object and the clock
# as arguments so the chain's decisions can be tested without Task Scheduler.

# How long an unfinished status file may go untouched before the chain that wrote it is presumed dead.
# Every step rewrites the file on finishing and no step runs longer than 30 minutes (2024-12-18 budgets).
$script:StaleRunMinutes = 45

function Get-ProcessedAt($Status) {
  # When today's rows were last judged and rendered: the status file's `processed` stamp (written after the
  # render step since 2024-12-20), else its `finished` stamp for files from before that, else $null.
  if (-not $Status) { return $null }
  foreach ($field in 'processed', 'finished') {
    $value = $Status.$field
    if ($value) { try { return [datetime]$value } catch { } }
  }
  return $null
}

function Resolve-Phase([string]$Phase, $Status, [string]$Today, [datetime]$Now, $RowsAt = $null) {
  # The phase the chain really runs, given today's status file (or $null) and the last-write time of
  # today's CSV ($RowsAt, $null when the file is absent).
  #   catchup -> 'exit' when today's run is under way; 'late' when today's run finished but rows were
  #     merged after the last processing pass (2024-12-20: the laptop slept through the 07:30 Late task
  #     and 151 feed rows were never judged or shown); 'exit' when finished and nothing new; else 'full'.
  #   full / late -> 'exit' while another chain for today is under way (2024-12-20: on a power-on day Task
  #     Scheduler starts the missed Night, Trickle and Catchup instances in the same second; the chain now
  #     writes its status at START so the others see it).
  #   trickle -> Wait = $true while the night run is under way (its processing steps must not overlap it).
  #   late / trickle -> 'full' when there is no night run for today (2024-12-19: a late pass on such a
  #     day used to write a finished status with no pull step, which silenced the catch-up and turned
  #     the banner green). A run under way (today's status touched within the stale window) keeps the phase.
  # Switched = $true when the chain was turned into a full run by this decision.
  $isToday = $Status -and ($Status.run_date -eq $Today)
  $updated = $null
  if ($isToday) { try { $updated = [datetime]$Status.updated } catch { } }
  $recent = $updated -and (($Now - $updated).TotalMinutes -lt $script:StaleRunMinutes)
  $hasPull = $false
  if ($isToday) { $hasPull = @($Status.steps | Where-Object { $_.name -eq 'pull' }).Count -gt 0 }

  if ($Phase -eq 'catchup') {
    if ($isToday -and $recent) {
      return @{ Phase = 'exit'; Switched = $false; Reason = 'today''s run is under way' }
    }
    if ($isToday -and $Status.finished) {
      $processed = Get-ProcessedAt $Status
      if ($RowsAt -and $processed -and ([datetime]$RowsAt -gt $processed)) {
        return @{ Phase = 'late'; Switched = $true; Reason = "rows merged at $(([datetime]$RowsAt).ToString('s')) after the last processing pass at $($processed.ToString('s'))" }
      }
      return @{ Phase = 'exit'; Switched = $false; Reason = 'today''s run finished and nothing arrived since' }
    }
    return @{ Phase = 'full'; Switched = $true; Reason = "no finished run recorded for $Today" }
  }
  $underWay = $isToday -and $recent -and -not $Status.finished
  if (($Phase -eq 'full' -or $Phase -eq 'late') -and $underWay) {
    return @{ Phase = 'exit'; Switched = $false; Reason = "another chain for $Today is under way (status touched $($updated.ToString('s')))" }
  }
  if ($Phase -eq 'late' -or $Phase -eq 'trickle') {
    if (-not $hasPull -and -not $recent) {
      return @{ Phase = 'full'; Switched = $true; Reason = "no night run for $Today; running the full chain instead of the $Phase pass" }
    }
  }
  if ($Phase -eq 'trickle' -and $underWay) {
    return @{ Phase = 'trickle'; Switched = $false; Wait = $true; Reason = "the night run is under way (status touched $($updated.ToString('s'))); waiting" }
  }
  return @{ Phase = $Phase; Switched = $false; Wait = $false; Reason = '' }
}

function Wait-ForChain([string]$StatusFile, [string]$Today, [int]$MaxMinutes = 60, [int]$PollSeconds = 30) {
  # Block while today's status is unfinished and fresh (another chain is running); return when it is finished,
  # stale, absent or from another day, or after $MaxMinutes. Returns the minutes waited. The trickle uses it so
  # its judge and render never overlap the night run's (2024-12-20).
  $started = Get-Date
  while (((Get-Date) - $started).TotalMinutes -lt $MaxMinutes) {
    $status = Read-StatusJson $StatusFile
    $r = Resolve-Phase 'trickle' $status $Today (Get-Date)
    if (-not $r.Wait) { break }
    Start-Sleep -Seconds $PollSeconds
  }
  return [int](((Get-Date) - $started).TotalMinutes)
}

function Write-JsonAtomic([string]$Path, [string]$Json) {
  # Write the whole file to a temp name in the same folder, then replace the target in one step, so a
  # reader (the catch-up decision, the render banner, cloud_sync) sees the old file or the new one and
  # never a half-written one. [IO.File]::Replace is the documented replacement call; a plain overwrite
  # (WriteAllText, the way until 2024-12-19) exposes a torn file for the length of the write.
  $dir = Split-Path -Parent $Path
  $tmp = Join-Path $dir ('.' + [IO.Path]::GetRandomFileName())
  [IO.File]::WriteAllText($tmp, $Json, [Text.UTF8Encoding]::new($false))
  # [NullString]::Value: Windows PowerShell turns a bare $null into "" for a string parameter, which Replace rejects.
  if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($tmp, $Path, [NullString]::Value) } else { [IO.File]::Move($tmp, $Path) }
}

function Read-StatusJson([string]$Path, [int]$RetrySeconds = 2) {
  # The parsed status file, or $null when it is absent. A parse failure is retried once after a pause:
  # with Write-JsonAtomic it should not happen, but a reader that treated a torn file as "no run" used
  # to start a second full chain (2024-12-19 review).
  for ($attempt = 0; $attempt -lt 2; $attempt++) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json) }
    catch { if ($attempt -eq 0) { Start-Sleep -Seconds $RetrySeconds } }
  }
  return $null
}

function Get-DeadChain($Status, [string]$Today, [datetime]$Now) {
  # When today's status file was left unfinished and untouched for the stale window, the chain that
  # wrote it died (killed at its task limit, a crash, a shutdown). Returns @{ LastStep; Updated } or
  # $null. 2024-12-17: the 20:00 catch-up chain died mid-judge and nothing anywhere recorded it; the
  # only trace was an orphan step output file.
  if (-not $Status -or $Status.run_date -ne $Today -or $Status.finished) { return $null }
  $updated = $null
  try { $updated = [datetime]$Status.updated } catch { return $null }
  if (($Now - $updated).TotalMinutes -lt $script:StaleRunMinutes) { return $null }
  $last = @($Status.steps | Select-Object -Last 1 | ForEach-Object { [string]$_.name })
  return @{ LastStep = $(if ($last.Count) { $last[0] } else { '(no step finished)' }); Updated = $updated.ToString('s') }
}

function Should-RunFeed([string]$Phase, [bool]$Switched) {
  # The LinkedIn feed belongs to the 01:00 trickle phase. A full run that is really a catch-up (sign-in,
  # hourly, or a late or trickle pass that found no night run) runs it too, or the missed night has no
  # LinkedIn rows until the next 01:00 (2024-12-19). The scheduled 00:30 full run never does: the feed's
  # exclusion list and budget are built for one batch a night at 01:00.
  return ($Phase -eq 'full' -and $Switched)
}

function Remove-SweepLeftovers([string]$Root, [string]$Today) {
  # Nightly hygiene (2024-12-19). Two kinds of leftover in weekly-sweeps: the merge's lock sidecars
  # (linkedin-raw-YYYY-MM-DD.csv.lock, one byte, never unlinked; 80 of them by October) from days other
  # than today, whose sidecar a step may still hold, and the *_filtered.csv copies nothing has written
  # since 2024-12-16 (72 files, 10 MB). Daily CSVs, logs and briefs are kept: the gold form and the
  # feed's exclusion list read the CSVs. Returns @{ Locks; Filtered } counts.
  $dir = Join-Path $Root 'weekly-sweeps'
  if (-not (Test-Path -LiteralPath $dir)) { return @{ Locks = 0; Filtered = 0 } }
  $locks = @(Get-ChildItem -LiteralPath $dir -Filter '*.csv.lock' -File | Where-Object { $_.Name -ne "linkedin-raw-$Today.csv.lock" })
  $filtered = @(Get-ChildItem -LiteralPath $dir -Filter '*_filtered.csv' -File)
  foreach ($f in @($locks) + @($filtered)) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
  return @{ Locks = $locks.Count; Filtered = $filtered.Count }
}

function Set-PythonStdio {
  # 2024-12-30: a redirected stdout is cp1252 on Windows. A zero-width space (U+200B) in a JSearch job
  # title made print() raise UnicodeEncodeError and the step exit 1, on the laptop and on the runner.
  # Keep the encoding, replace what it cannot encode with an escape. Every child step inherits this
  # through Start-Process; python -I would ignore it, and no step runs with -I.
  $env:PYTHONIOENCODING = ':backslashreplace'
}
