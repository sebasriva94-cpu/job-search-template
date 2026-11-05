# Pester 3.x. Run from the Job Search root:
#   powershell -NoProfile -Command "Invoke-Pester -Path tests\pull-jobs.lib.Tests.ps1"
. (Join-Path $PSScriptRoot '..\scripts\automation\pull-jobs.lib.ps1')

$today = '2024-12-19'
$now = [datetime]'2024-12-19T07:30:00'

function Status([string]$RunDate, [string[]]$Steps, $Finished, [string]$Updated) {
  [pscustomobject]@{
    run_date = $RunDate
    updated  = $Updated
    finished = $Finished
    steps    = @($Steps | ForEach-Object { [pscustomobject]@{ name = $_; exit = 0 } })
  }
}

Describe 'Resolve-Phase: catch-up' {
  It 'exits when today''s run finished' {
    $r = Resolve-Phase 'catchup' (Status $today @('pull','judge') '2024-12-19T00:43:28' '2024-12-19T00:43:28') $today $now
    $r.Phase | Should Be 'exit'
  }
  It 'exits when today''s run is under way (touched within 45 minutes)' {
    $r = Resolve-Phase 'catchup' (Status $today @('pull') $null '2024-12-19T07:10:00') $today $now
    $r.Phase | Should Be 'exit'
  }
  It 'runs the full chain when today''s unfinished run went stale' {
    $r = Resolve-Phase 'catchup' (Status $today @('pull','judge') $null '2024-12-19T02:00:00') $today $now
    $r.Phase | Should Be 'full'
    $r.Switched | Should Be $true
  }
  It 'runs the full chain when the status file is from yesterday or missing' {
    (Resolve-Phase 'catchup' (Status '2024-12-18' @('pull') '2024-12-18T00:43:28' '2024-12-18T00:43:28') $today $now).Phase | Should Be 'full'
    (Resolve-Phase 'catchup' $null $today $now).Phase | Should Be 'full'
  }
}

Describe 'Resolve-Phase: catch-up when rows arrived after the last processing pass' {
  # 2024-12-20: the trickle merged 162 feed rows at 01:04, the laptop slept through the 07:30 Late task, and the
  # catch-up exited all day because the status said finished. Now it compares the CSV's write time with `processed`.
  function Done([string]$Finished, $Processed) {
    $s = Status $today @('pull','judge','render') $Finished $Finished
    if ($null -ne $Processed) { $s | Add-Member -NotePropertyName processed -NotePropertyValue $Processed }
    return $s
  }
  It 'exits when the CSV is older than the processed stamp' {
    $r = Resolve-Phase 'catchup' (Done '2024-12-19T01:05:34' '2024-12-19T01:05:00') $today $now ([datetime]'2024-12-19T01:04:31')
    $r.Phase | Should Be 'exit'
  }
  It 'runs the late pass when rows were merged after the processed stamp' {
    $r = Resolve-Phase 'catchup' (Done '2024-12-19T01:05:34' '2024-12-19T00:45:00') $today $now ([datetime]'2024-12-19T01:04:31')
    $r.Phase | Should Be 'late'
    $r.Switched | Should Be $true
    $r.Reason | Should Match 'rows merged at'
  }
  It 'falls back to the finished stamp for a status file without processed' {
    $r = Resolve-Phase 'catchup' (Done '2024-12-19T00:45:00' $null) $today $now ([datetime]'2024-12-19T01:04:31')
    $r.Phase | Should Be 'late'
  }
  It 'exits when there is no CSV for today' {
    (Resolve-Phase 'catchup' (Done '2024-12-19T00:45:00' '2024-12-19T00:45:00') $today $now $null).Phase | Should Be 'exit'
  }
  It 'exits while a run is under way even if rows are newer (that run will process them)' {
    $s = Status $today @('pull') $null '2024-12-19T07:10:00'
    (Resolve-Phase 'catchup' $s $today $now ([datetime]'2024-12-19T07:12:00')).Phase | Should Be 'exit'
  }
}

Describe 'Resolve-Phase: one chain at a time (2024-12-20)' {
  # On a power-on day Task Scheduler starts the missed Night, Trickle and Catchup instances in the same second.
  $underWay = Status $today @('pull') $null '2024-12-19T07:28:00'
  It 'full exits while another chain for today is under way' {
    $r = Resolve-Phase 'full' $underWay $today $now
    $r.Phase | Should Be 'exit'
    $r.Reason | Should Match 'under way'
  }
  It 'late exits while another chain for today is under way' {
    (Resolve-Phase 'late' $underWay $today $now).Phase | Should Be 'exit'
  }
  It 'trickle waits while the night run is under way, then keeps its phase' {
    $r = Resolve-Phase 'trickle' $underWay $today $now
    $r.Phase | Should Be 'trickle'
    $r.Wait | Should Be $true
    $done = Status $today @('pull','judge','render') '2024-12-19T07:29:00' '2024-12-19T07:29:00'
    (Resolve-Phase 'trickle' $done $today $now).Wait | Should Be $false
  }
  It 'full still runs when today has no status, a finished one, or a stale unfinished one' {
    (Resolve-Phase 'full' $null $today $now).Phase | Should Be 'full'
    (Resolve-Phase 'full' (Status $today @('pull') '2024-12-19T00:45:00' '2024-12-19T00:45:00') $today $now).Phase | Should Be 'full'
    (Resolve-Phase 'full' (Status $today @('pull') $null '2024-12-19T02:00:00') $today $now).Phase | Should Be 'full'
  }
  It 'Wait-ForChain returns at once when nothing is under way' {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ('wait-' + [IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $dir | Out-Null
    $file = Join-Path $dir 'pipeline-status.json'
    Write-JsonAtomic $file ('{"run_date":"' + $today + '","updated":"2024-12-19T00:45:00","finished":"2024-12-19T00:45:00","steps":[]}')
    (Wait-ForChain $file $today 1 1) | Should Be 0
    Remove-Item -LiteralPath $dir -Recurse -Force
  }
}

Describe 'Resolve-Phase: late and trickle on a day with no night run' {
  It 'late becomes full when today has no status at all' {
    $r = Resolve-Phase 'late' $null $today $now
    $r.Phase | Should Be 'full'
    $r.Switched | Should Be $true
  }
  It 'late becomes full when the status file is yesterday''s' {
    $r = Resolve-Phase 'late' (Status '2024-12-18' @('pull','judge') '2024-12-18T00:43:28' '2024-12-18T00:43:28') $today $now
    $r.Phase | Should Be 'full'
  }
  It 'trickle becomes full when today''s status has no pull step and is stale' {
    $r = Resolve-Phase 'trickle' (Status $today @('linkedin-feed') '2024-12-19T01:04:31' '2024-12-19T01:04:31') $today ([datetime]'2024-12-19T07:30:00')
    $r.Phase | Should Be 'full'
  }
  It 'late keeps its phase when the night run exists' {
    $r = Resolve-Phase 'late' (Status $today @('pull','judge','render') '2024-12-19T00:43:28' '2024-12-19T00:43:28') $today $now
    $r.Phase | Should Be 'late'
    $r.Switched | Should Be $false
  }
  It 'trickle keeps its phase while the night run is still under way' {
    $r = Resolve-Phase 'trickle' (Status $today @('target') $null '2024-12-19T00:58:00') $today ([datetime]'2024-12-19T01:00:00')
    $r.Phase | Should Be 'trickle'
  }
  It 'full and process are never changed' {
    (Resolve-Phase 'full' $null $today $now).Phase | Should Be 'full'
    (Resolve-Phase 'process' $null $today $now).Phase | Should Be 'process'
  }
}

Describe 'Write-JsonAtomic and Read-StatusJson' {
  $dir = Join-Path ([IO.Path]::GetTempPath()) ('pulljobs-' + [IO.Path]::GetRandomFileName())
  New-Item -ItemType Directory -Path $dir | Out-Null
  $file = Join-Path $dir 'pipeline-status.json'
  It 'creates the file and leaves no temp file behind' {
    Write-JsonAtomic $file '{"run_date":"2024-12-19","steps":[]}'
    (Read-StatusJson $file).run_date | Should Be '2024-12-19'
    @(Get-ChildItem $dir -Force).Count | Should Be 1
  }
  It 'replaces an existing file in place and still leaves one file' {
    Write-JsonAtomic $file '{"run_date":"2024-12-20","steps":[]}'
    (Read-StatusJson $file).run_date | Should Be '2024-12-20'
    @(Get-ChildItem $dir -Force).Count | Should Be 1
  }
  It 'writes UTF-8 without a byte order mark' {
    $bytes = [IO.File]::ReadAllBytes($file)
    $bytes[0] | Should Be ([byte][char]'{')
  }
  It 'reads an absent file as null' {
    (Read-StatusJson (Join-Path $dir 'missing.json')) | Should Be $null
  }
  It 'reads a truncated file as null only after one retry' {
    [IO.File]::WriteAllText($file, '{"run_date":"2024-12-2')
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $result = Read-StatusJson $file 1
    $watch.Stop()
    $result | Should Be $null
    ($watch.Elapsed.TotalSeconds -ge 1) | Should Be $true
  }
  Remove-Item $dir -Recurse -Force
}

Describe 'Get-DeadChain: a chain that died leaves a record' {
  It 'names the last finished step of a stale unfinished run' {
    $dead = Get-DeadChain (Status $today @('pull','target','hubspot') $null '2024-12-19T20:03:00') $today ([datetime]'2024-12-19T21:00:00')
    $dead.LastStep | Should Be 'hubspot'
    $dead.Updated | Should Be '2024-12-19T20:03:00'
  }
  It 'is null for a finished run, a fresh run, yesterday''s file or no file' {
    (Get-DeadChain (Status $today @('pull') '2024-12-19T00:43:28' '2024-12-19T00:43:28') $today $now) | Should Be $null
    (Get-DeadChain (Status $today @('pull') $null '2024-12-19T07:10:00') $today $now) | Should Be $null
    (Get-DeadChain (Status '2024-12-18' @('pull') $null '2024-12-18T20:03:00') $today $now) | Should Be $null
    (Get-DeadChain $null $today $now) | Should Be $null
  }
  It 'says so when no step had finished' {
    (Get-DeadChain (Status $today @() $null '2024-12-19T00:31:00') $today $now).LastStep | Should Be '(no step finished)'
  }
}

Describe 'Should-RunFeed: the LinkedIn feed in catch-up runs' {
  It 'runs the feed when a full run came from a switch (catch-up, or late or trickle with no night run)' {
    Should-RunFeed 'full' $true | Should Be $true
  }
  It 'does not run the feed in the scheduled 00:30 full run' {
    Should-RunFeed 'full' $false | Should Be $false
  }
  It 'never runs it from the late or process phases' {
    Should-RunFeed 'late' $false | Should Be $false
    Should-RunFeed 'process' $false | Should Be $false
  }
  It 'the decision table and the feed rule agree on a missed night' {
    $r = Resolve-Phase 'late' $null $today $now
    Should-RunFeed $r.Phase $r.Switched | Should Be $true
  }
}

Describe 'Remove-SweepLeftovers' {
  It 'removes old lock sidecars and _filtered copies, keeps today''s lock and every daily CSV' {
    $root = Join-Path ([IO.Path]::GetTempPath()) ("prune-" + [guid]::NewGuid().ToString('n'))
    $dir = New-Item -ItemType Directory -Path (Join-Path $root 'weekly-sweeps')
    foreach ($name in @('linkedin-raw-2024-12-19.csv', 'linkedin-raw-2024-12-19.csv.lock', 'linkedin-raw-2024-09-16.csv',
                        'linkedin-raw-2024-09-16.csv.lock', 'linkedin-raw-2024-10-29_filtered.csv')) {
      Set-Content -LiteralPath (Join-Path $dir $name) -Value '0'
    }
    $r = Remove-SweepLeftovers $root '2024-12-19'
    $r.Locks | Should Be 1
    $r.Filtered | Should Be 1
    $left = @(Get-ChildItem -LiteralPath $dir | Sort-Object Name | ForEach-Object Name)
    ($left -join ',') | Should Be 'linkedin-raw-2024-09-16.csv,linkedin-raw-2024-12-19.csv,linkedin-raw-2024-12-19.csv.lock'
    Remove-Item -LiteralPath $root -Recurse -Force
  }
  It 'returns zeros when the folder is missing' {
    $r = Remove-SweepLeftovers (Join-Path ([IO.Path]::GetTempPath()) 'no-such-root') '2024-12-19'
    ($r.Locks + $r.Filtered) | Should Be 0
  }
}

Describe 'Set-PythonStdio (2024-12-30)' {
  # A zero-width space in a JSearch title crashed print() on the chain's cp1252 redirected stdout and the
  # step exited 1 on both machines. This file and the probe stay pure ASCII on purpose: Windows PowerShell
  # 5.1 reads a BOM-less .ps1 as ANSI, so a literal U+200B here would be mangled into cp1252-encodable bytes
  # and both tests would pass for the wrong reason. The probe source carries the Python escape sequence
  # (backslash, u, 2, 0, 0, b), built from a codepoint so the escape itself is never typed, and Python
  # creates the real character at run time. backslashreplace writes that same six-character escape back out.
  $python = if ($env:JOBSEARCH_PYTHON) { $env:JOBSEARCH_PYTHON } else { Join-Path $PSScriptRoot '..\scripts\automation\venv\Scripts\python.exe' }
  $escape = 'x' + [char]92 + 'u200by'          # the ASCII text x\u200by, without typing the escape
  $probeSource = 'print("' + $escape + '")'

  function Invoke-Probe([string]$Interpreter, [string]$Source) {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("stdio-probe-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $dir | Out-Null
    try {
      $probe = Join-Path $dir 'probe.py'
      Set-Content -Path $probe -Encoding Ascii -Value $Source
      $out = Join-Path $dir 'out.txt'
      $err = Join-Path $dir 'err.txt'
      $proc = Start-Process -FilePath $Interpreter -ArgumentList @($probe) -RedirectStandardOutput $out `
        -RedirectStandardError $err -PassThru -Wait -WindowStyle Hidden
      return @{ Exit = $proc.ExitCode
                Out = $(if (Test-Path $out) { Get-Content $out -Raw } else { '' })
                Err = $(if (Test-Path $err) { Get-Content $err -Raw } else { '' }) }
    } finally {
      Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
  }

  It 'lets a Python step print a character cp1252 lacks' {
    if (-not (Test-Path -LiteralPath $python)) { Set-TestInconclusive "no interpreter at $python" }
    $saved = $env:PYTHONIOENCODING
    try {
      Set-PythonStdio
      $env:PYTHONIOENCODING | Should Be ':backslashreplace'
      $r = Invoke-Probe $python $probeSource
      $r.Exit | Should Be 0
      $r.Out.Trim() | Should Be $escape
    } finally {
      if ($null -eq $saved) { Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue } else { $env:PYTHONIOENCODING = $saved }
    }
  }

  It 'without it the same print fails (the negative control)' {
    if (-not (Test-Path -LiteralPath $python)) { Set-TestInconclusive "no interpreter at $python" }
    $saved = $env:PYTHONIOENCODING
    try {
      Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
      $r = Invoke-Probe $python $probeSource
      $r.Exit | Should Be 1
      ($r.Err -match 'UnicodeEncodeError') | Should Be $true
    } finally {
      if ($null -ne $saved) { $env:PYTHONIOENCODING = $saved }
    }
  }
}
