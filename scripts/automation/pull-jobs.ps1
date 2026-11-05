# Nightly discovery -> judge -> dashboard pipeline with bounded steps; pipeline-status.json is the alert.
#
# Phases: full (the 00:30 night task: pull, sweeps, judge, checks, render), pull / process (a manual
# split run; process carries the pull record from the status file), late (run by the hourly catch-up since
# 2024-12-20 when rows were merged after the last render: judge, queue-check and render again; the night's
# step records are carried, not replaced) and catchup (at sign-in, unlock, resume and hourly by day: exits at
# once when today's run is under way or finished with nothing new, runs the late pass when today's CSV is newer
# than the status file's processed stamp, else runs the full chain, so a night the laptop was off is made up
# the first time it is awake; 10/20 was such a night).
# late and trickle turn into the full chain themselves when today has no night run (2024-12-19).
# trickle (the 01:00 task since 2024-12-17): the LinkedIn lane alone. Since 2024-12-18 that is the Bright
# Data job-listings feed (linkedin_feed.py, one batch, 300 records a night at most); the guest-endpoint
# trickle (linkedin_pull.py --trickle, 16 groups spread over the night with escalating 429 pauses) runs only
# as its fallback. Its record joins the night's in the status file. The full phase pulls everything but LinkedIn.
param([ValidateSet('full','pull','process','late','catchup','trickle')][string]$Phase = 'full')
$ErrorActionPreference = 'Continue'

# The scheduled task's venv must not inherit an ambient Python package path. In particular,
# a parent agent's PYTHONPATH can put a foreign pydantic ahead of this venv and make the
# preflight fail before any child step launches.
$env:PYTHONPATH = $null
$env:PYTHONHOME = $null

$defaultRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$root = if ($env:JOBSEARCH_ROOT) { $env:JOBSEARCH_ROOT } else { $defaultRoot }
# 2024-12-23: the chain also runs on a GitHub Actions Windows runner (.github/workflows/night-chain.yml), where the
# checkout is $env:JOBSEARCH_ROOT and the interpreter is $env:JOBSEARCH_PYTHON; on the laptop both default to the
# Job Search folder and its venv. Every script path below follows $root for the same reason.
$py = if ($env:JOBSEARCH_PYTHON) { $env:JOBSEARCH_PYTHON } else { Join-Path $root 'scripts\automation\venv\Scripts\python.exe' }
$logDir = Join-Path $root 'automation-logs'
$stepDir = Join-Path $logDir 'steps'
$monthLog = Join-Path $logDir ("linkedin-pull-{0}.log" -f (Get-Date -Format 'yyyy-MM'))
$testMode = [bool]($env:JOBSEARCH_TEST_FAIL_STEP -or $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -or $env:JOBSEARCH_TEST_SMTP_FAIL -or $env:JOBSEARCH_ALERT_MARKER_DIR)
$prefix = if ($testMode) { '[TEST] ' } else { '' }
New-Item -ItemType Directory -Force -Path $logDir, $stepDir | Out-Null

# Which phase really runs (scripts\automation\pull-jobs.lib.ps1, tested by tests\pull-jobs.lib.Tests.ps1):
# catchup exits when today's run finished or is under way (status touched within 45 min; 2024-12-17: the
# 20:00 catch-up chain died mid-judge and a 3-hour grace hid the dead run until 00:30), else runs the full
# chain. late and trickle become the full chain when there is no night run for today (2024-12-19: a late
# pass on such a day wrote a finished status with no pull step, which silenced the catch-up).
. (Join-Path $PSScriptRoot 'pull-jobs.lib.ps1')
Set-PythonStdio   # 2024-12-30: a character cp1252 lacks no longer kills a Python step (tests\pull-jobs.lib.Tests.ps1)
$statusFile = Join-Path $logDir 'pipeline-status.json'
$todayStamp = Get-Date -Format 'yyyy-MM-dd'
$prior = Read-StatusJson $statusFile
$csvToday = Join-Path $root "weekly-sweeps\linkedin-raw-$todayStamp.csv"
$rowsAt = if (Test-Path -LiteralPath $csvToday) { (Get-Item -LiteralPath $csvToday).LastWriteTime } else { $null }
# 2024-12-26: on the Actions runner the CSV was just restored from the state branch, so its write time is the
# restore time and every catch-up after a finished night ran a late pass; there no rows arrive outside a chain.
if ($env:GITHUB_ACTIONS -eq 'true') { $rowsAt = $null }
$decision = Resolve-Phase $Phase $prior $todayStamp (Get-Date) $rowsAt
if ($decision.Phase -eq 'exit') {
  if ($decision.Reason -like 'another chain*') { Add-Content $monthLog "[$(Get-Date -Format o)] $($Phase.ToUpper()): $($decision.Reason); exiting" }
  exit 0
}
if ($decision.Switched) {
  Add-Content $monthLog "[$(Get-Date -Format o)] $($Phase.ToUpper()): $($decision.Reason)"
}
if ($decision.Wait) {
  Add-Content $monthLog "[$(Get-Date -Format o)] TRICKLE: $($decision.Reason)"
  $waited = Wait-ForChain $statusFile $todayStamp 60
  Add-Content $monthLog "[$(Get-Date -Format o)] TRICKLE: waited $waited min for the night run"
  $prior = Read-StatusJson $statusFile
}
$switched = [bool]$decision.Switched
$Phase = $decision.Phase
$stepSuffix = if ($Phase -eq 'late') { '-late' } elseif ($Phase -eq 'trickle') { '-trickle' } else { '' }
$processedAt = $null   # set after the render step; the catch-up compares it with the CSV's write time
Get-ChildItem $logDir -Filter 'linkedin-pull-*.log' -File -ErrorAction SilentlyContinue |
  Where-Object LastWriteTime -lt (Get-Date).AddMonths(-6) | Remove-Item -Force

function Get-UserEnv([string]$Name) {
  $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
  if (-not $value) { $value = [Environment]::GetEnvironmentVariable($Name, 'User') }
  return $value
}

function Sanitize-Line([string]$Text) {
  if (-not $Text) { return '' }
  $safe = $Text -replace '(?i)\b(?:https?|wss?)://\S+', '[redacted-url]'
  $safe = $safe -replace '(?i)\b(?:token|password|authorization|api[_-]?key)\s*[=:]\s*\S+', '[redacted-secret]'
  $safe = $safe -replace '[\x00-\x1f\x7f]', ' '
  if ($safe.Length -gt 200) { $safe = $safe.Substring(0, 200) }
  return $safe.Trim()
}

# A step carries a State next to its exit code (2024-12-11): ok, degraded (a provider was throttled,
# paused or skipped but the step still did useful work) or failed (non-zero exit, or a provider
# reported failed, e.g. a rejected token). Providers is the sanitized summary the Python step printed
# on its PROVIDER_STATUS line (see scripts\rate_control.py): name -> state, reason, cooldown_until,
# consecutive_degraded_runs. Nothing else from the step's output is copied into the status file.
function ConvertTo-ProviderSummary($Raw) {
  $out = [ordered]@{}
  if (-not $Raw) { return $out }
  foreach ($prop in @($Raw.PSObject.Properties | Select-Object -First 20)) {
    $item = $prop.Value
    if (-not $item) { continue }
    $until = $item.cooldown_until
    if ($until -is [datetime]) { $until = $until.ToString('yyyy-MM-ddTHH:mm:sszzz') }
    $entry = [ordered]@{
      state  = (Sanitize-Line ([string]$item.state))
      reason = (Sanitize-Line ([string]$item.reason))
    }
    if ($until) { $entry['cooldown_until'] = (Sanitize-Line ([string]$until)) }
    $runs = 0
    if ([int]::TryParse([string]$item.consecutive_degraded_runs, [ref]$runs)) { $entry['consecutive_degraded_runs'] = $runs }
    $out[(Sanitize-Line ([string]$prop.Name))] = $entry
  }
  return $out
}

function Get-StepState([int]$ExitCode, $Providers) {
  if ($ExitCode -ne 0) { return 'failed' }
  $states = @()
  if ($Providers) { $states = @($Providers.Values | ForEach-Object { $_['state'] }) }
  if ($states -contains 'failed') { return 'failed' }
  if (@($states | Where-Object { $_ -and $_ -ne 'ok' }).Count) { return 'degraded' }
  return 'ok'
}

function New-StepRecord([string]$Name, [int]$ExitCode, [bool]$TimedOut, [double]$DurationSec,
                        [Nullable[int]]$RowCount, [string]$FirstErrorLine, $Providers = $null) {
  $summary = if ($Providers) { $Providers } else { [ordered]@{} }
  [pscustomobject]@{ Name=$Name; ExitCode=$ExitCode; TimedOut=$TimedOut;
    DurationSec=[math]::Round($DurationSec,1); RowCount=$RowCount;
    FirstErrorLine=(Sanitize-Line $FirstErrorLine);
    State=(Get-StepState $ExitCode $summary); Providers=$summary }
}

function Invoke-Step {
  param([string]$Name, [string]$File, [string[]]$ArgumentList, [int]$TimeoutMinutes,
        [string]$WorkingDirectory = $root)
  $outFile = Join-Path $stepDir ("{0}-{1}.out.txt" -f (Get-Date -Format 'yyyy-MM-dd'), $Name)
  $errFile = Join-Path $stepDir ("{0}-{1}.err.txt" -f (Get-Date -Format 'yyyy-MM-dd'), $Name)
  Remove-Item $outFile,$errFile -Force -ErrorAction SilentlyContinue
  $watch = [Diagnostics.Stopwatch]::StartNew()
  $timedOut = $false
  $exitCode = -1
  try {
    $quoted = @($ArgumentList | ForEach-Object {
      if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    })
    $proc = Start-Process -FilePath $File -ArgumentList $quoted -WorkingDirectory $WorkingDirectory `
      -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru -WindowStyle Hidden
    $null = $proc.Handle
    if (-not $proc.WaitForExit($TimeoutMinutes * 60000)) {
      $timedOut = $true
      & taskkill.exe /PID $proc.Id /T /F | Out-Null
    }
    $proc.WaitForExit()
    $rawExit = $proc.ExitCode
    if ($timedOut) { $exitCode = 124 }
    elseif ($null -eq $rawExit) { $exitCode = 126 }
    else { $exitCode = [int]$rawExit }
  } catch {
    $exitCode = 125
    Set-Content -Path $errFile -Value $_.Exception.Message
  }
  $watch.Stop()
  $stdout = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { '' }
  $stderr = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { '' }
  $count = $null
  if ($stdout -match '(?im)Total unique:\s*(\d+)') { $count = [int]$Matches[1] }
  elseif ($stdout -match '(?im)(\d+)\s+IT-relevant') { $count = [int]$Matches[1] }
  elseif ($stdout -match '(?im)"review_count":\s*(\d+)') {
    # The dashboard has split the surfaced queue into two tiers since 2024-10-29. Count both, or a
    # morning where every checked candidate lands in Apply-now reads as an empty queue (Sep 2 to 8).
    $count = [int]$Matches[1]
    if ($stdout -match '(?im)"apply_now_count":\s*(\d+)') { $count += [int]$Matches[1] }
  }
  $providers = $null
  $statusLine = @(($stdout -split "`r?`n") | Where-Object { $_ -like 'PROVIDER_STATUS {*' }) | Select-Object -Last 1
  if ($statusLine) {
    try { $providers = ConvertTo-ProviderSummary ((($statusLine -replace '^PROVIDER_STATUS ', '') | ConvertFrom-Json).providers) }
    catch { $providers = $null }
  }
  $firstError = (($stderr -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1))
  if (-not $firstError) {
    $firstError = (($stdout -split "`r?`n" | Where-Object { $_ -match '(?i)failed|error|timeout' } | Select-Object -First 1))
  }
  Add-Content $monthLog "[$(Get-Date -Format o)] STEP $Name exit=$exitCode timeout=$timedOut duration=$($watch.Elapsed.TotalSeconds)"
  foreach ($line in (($stdout + "`n" + $stderr) -split "`r?`n")) {
    $safe = Sanitize-Line $line
    if ($safe) { Add-Content $monthLog "[$Name] $safe" }
  }
  Remove-Item $outFile,$errFile -Force -ErrorAction SilentlyContinue
  return New-StepRecord $Name $exitCode $timedOut $watch.Elapsed.TotalSeconds $count $firstError $providers
}

function Invoke-Injected([string]$Name) {
  return New-StepRecord $Name 99 $false 0 $null 'injected test failure'
}
function Invoke-Noop([string]$Name) {
  return New-StepRecord $Name 0 $false 0 0 ''
}
function Is-Injected([string]$Name) {
  $names = @($env:JOBSEARCH_TEST_FAIL_STEP -split ',' | ForEach-Object { $_.Trim().ToLower() })
  return $names -contains $Name.ToLower()
}
function Run-Or-Inject([string]$Name,[string]$File,[string[]]$StepArgs,[int]$Minutes,[bool]$Paid=$true,[string]$Cwd=$root) {
  if (Is-Injected $Name) { return Invoke-Injected $Name }
  if ($Paid -and $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -eq '1') { return Invoke-Noop $Name }
  return Invoke-Step $Name $File $StepArgs $Minutes $Cwd
}

Add-Content $monthLog "[$(Get-Date -Format o)] ${prefix}START root=$root"
$results = [Collections.Generic.List[object]]::new()

# A chain that died (killed at its task limit, a crash, a shutdown) left today's status unfinished and
# untouched for 45 minutes. Say so in the log and carry a failed chain-died step, so the alert record and
# the banner turn red instead of the death leaving no trace (2024-12-17: nothing recorded the 20:00 death).
$dead = Get-DeadChain $prior $todayStamp (Get-Date)
if ($dead) {
  Add-Content $monthLog "[$(Get-Date -Format o)] ${prefix}previous chain for $todayStamp died after step $($dead.LastStep); status last touched $($dead.Updated)"
  $results.Add((New-StepRecord 'chain-died' 1 $false 0 $null "previous chain died after step $($dead.LastStep); status last touched $($dead.Updated)"))
}

# Pipeline health record, rewritten after every step and again after the alert. The dashboard
# (scripts\job_dashboard.py) renders it as a banner, so a failed step or a failed alert email is
# visible in job-dashboard.html itself even when no email arrives.
$runStarted = Get-Date -Format o
$alertRecord = @{ status = 'none'; detail = '' }
function Write-Status([string]$Finished = '') {
  $steps = @($results | ForEach-Object {
    @{ name = $_.Name; exit = $_.ExitCode; timed_out = $_.TimedOut; duration_sec = $_.DurationSec
       rows = $_.RowCount; error = $_.FirstErrorLine; state = $_.State; providers = $_.Providers }
  })
  $status = [ordered]@{
    run_date  = (Get-Date -Format 'yyyy-MM-dd')
    started   = $runStarted
    updated   = (Get-Date -Format o)
    finished  = $(if ($Finished) { $Finished } else { $null })
    processed = $processedAt
    root      = $root
    test_mode = $testMode
    steps     = $steps
    failures  = @($results | Where-Object { $_.ExitCode -ne 0 } | ForEach-Object { $_.Name })
    degraded  = @($results | Where-Object { $_.ExitCode -eq 0 -and $_.State -eq 'degraded' } | ForEach-Object { $_.Name })
    alert     = $alertRecord
  }
  try {
    $json = $status | ConvertTo-Json -Depth 6
    Write-JsonAtomic $statusFile $json
  } catch {
    Add-Content $monthLog "[$(Get-Date -Format o)] status write failed: $(Sanitize-Line $_.Exception.Message)"
  }
}
function Read-PullRecord {
  # -Phase process: the 00:30 pull task wrote pipeline-status.json; carry its pull step into this
  # run so the dashboard banner and the alert still see a failed, timed-out or missing pull.
  if ($env:JOBSEARCH_TEST_NO_PAID_SWEEPS -eq '1') { return Invoke-Noop 'pull' }
  try {
    $prior = Read-StatusJson $statusFile
    if ($prior -and $prior.run_date -eq (Get-Date -Format 'yyyy-MM-dd')) {
      $step = @($prior.steps | Where-Object { $_.name -eq 'pull' }) | Select-Object -First 1
      if ($step) {
        return New-StepRecord 'pull' ([int]$step.exit) ([bool]$step.timed_out) ([double]$step.duration_sec) $step.rows ([string]$step.error) (ConvertTo-ProviderSummary $step.providers)
      }
    }
  } catch { }
  return New-StepRecord 'pull' 2 $false 0 $null 'no pull record from the 00:30 task today'
}
function Read-PriorRecords {
  # -Phase late: carry every step the night run recorded today, so the status file (and the banner
  # and the brief that read it) keeps the night's failures next to the late steps.
  $out = @()
  try {
    $prior = Read-StatusJson $statusFile
    if ($prior -and $prior.run_date -eq (Get-Date -Format 'yyyy-MM-dd')) {
      foreach ($step in @($prior.steps)) {
        $out += New-StepRecord ([string]$step.name) ([int]$step.exit) ([bool]$step.timed_out) ([double]$step.duration_sec) $step.rows ([string]$step.error) (ConvertTo-ProviderSummary $step.providers)
      }
    }
  } catch { }
  return $out
}

# START (2024-12-20): carry the records this phase builds on, then write the status file before any step runs,
# so every other chain started in the same minute (a power-on day fires the missed Night, Trickle and Catchup
# instances together) sees today's run as under way and exits. Until now the first write came after the pull.
if ($Phase -eq 'process') { $results.Add((Read-PullRecord)) }
elseif ($Phase -eq 'late' -or $Phase -eq 'trickle') { foreach ($record in @(Read-PriorRecords)) { $results.Add($record) } }
Write-Status

$pythonOk = (Test-Path $py)
if ($pythonOk) {
  & $py -c 'import pandas,jobspy' 2>$null
  $pythonOk = ($LASTEXITCODE -eq 0)
}

if (-not $pythonOk) {
  foreach ($name in 'pull','target','usajobs','hubspot','render') {
    $results.Add((New-StepRecord $name 127 $false 0 $null 'Python preflight failed'))
  }
} elseif ($Phase -eq 'process') {
  # the pull record was loaded at START
} elseif ($Phase -eq 'late') {
  # the night's records were loaded at START
} elseif ($Phase -eq 'trickle') {
  # 2024-12-18: the LinkedIn lane is the Bright Data job-listings feed (scripts\linkedin_feed.py): one batch of the
  # 16 query groups, about 2 min, capped at 300 records a night by rate_control.BRIGHTDATA_FEED. The guest-endpoint
  # trickle (linkedin_pull.py --trickle, 300 min: 16 groups 12-20 min apart plus 429 pauses and the detail fetches)
  # is the fallback: it runs when the feed step exits non-zero, or every night while JOBSEARCH_LINKEDIN_LANE=guest.
  $lane = "$env:JOBSEARCH_LINKEDIN_LANE".ToLower()
  $feed = $null
  if ($lane -ne 'guest') {
    $feed = Run-Or-Inject 'linkedin-feed' $py @((Join-Path $root 'scripts\linkedin_feed.py')) 15 $true
    $results.Add($feed)
  }
  if ($lane -eq 'guest' -or $feed.ExitCode -ne 0) {
    $why = if ($lane -eq 'guest') { 'JOBSEARCH_LINKEDIN_LANE=guest' } else { "feed step exit=$($feed.ExitCode)" }
    Add-Content $monthLog "[$(Get-Date -Format o)] ${prefix}LinkedIn guest lane runs ($why)"
    $results.Add((Run-Or-Inject 'linkedin' $py @((Join-Path $root 'scripts\linkedin_pull.py'),'100','--trickle') 300 $true))
  }
} else {
  # 60 min (2024-12-13): 16 LinkedIn sections of three pages, 60-120 s apart, run about 30 min on a
  # normal night, plus Indeed and the APIs; one in-run 429 pause adds 10-15 min. The pull saves per
  # section, so a run cut by this cap still keeps what it found.
  # Fast lane (2024-12-17): Indeed and the APIs only, about five minutes; LinkedIn runs in the 01:00 trickle task.
  $results.Add((Run-Or-Inject 'pull' $py @((Join-Path $root 'scripts\linkedin_pull.py'),'100','--no-linkedin') 20 $true))
}
if ($pythonOk -and ($Phase -eq 'full' -or $Phase -eq 'process')) {
  $results.Add((Run-Or-Inject 'target' $py @((Join-Path $root 'scripts\target_sweep.py')) 5 $true))
  $usaKey = Get-UserEnv 'USAJOBS_API_KEY'
  if (-not $usaKey -and $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -ne '1') {
    $results.Add((New-StepRecord 'usajobs' 127 $false 0 $null 'USAJOBS_API_KEY missing'))
  } else { $results.Add((Run-Or-Inject 'usajobs' $py @((Join-Path $root 'scripts\usajobs_sweep.py')) 5 $true)) }
  # JSearch (Google's job listings, added 2024-12-10): 3 searches once a day on the owner's free OpenWeb Ninja plan.
  $jsKey = Get-UserEnv 'Jsearch_API_TOKEN'
  if (-not $jsKey -and $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -ne '1') {
    $results.Add((New-StepRecord 'jsearch' 127 $false 0 $null 'Jsearch_API_TOKEN missing'))
  } else { $results.Add((Run-Or-Inject 'jsearch' $py @((Join-Path $root 'scripts\jsearch_sweep.py')) 5 $true)) }
  $bdKey = Get-UserEnv 'BRIGHTDATA_API_TOKEN'
  & $py -c 'import playwright' 2>$null; $playwrightOk = ($LASTEXITCODE -eq 0)
  if ((-not $bdKey -or -not $playwrightOk) -and $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -ne '1') {
    $results.Add((New-StepRecord 'hubspot' 127 $false 0 $null 'HubSpot prerequisites missing'))
  } else { $results.Add((Run-Or-Inject 'hubspot' $py @((Join-Path $root 'scripts\hubspot_sweep.py')) 25 $true)) }
  # A full run that is really a catch-up (sign-in, hourly, or a late or trickle pass that found no night run)
  # also runs the LinkedIn feed, or the missed night has no LinkedIn rows until the next 01:00 (2024-12-19).
  # rate_control.BRIGHTDATA_FEED refuses a second run the same day (a clean exit 0), so this never buys twice.
  if (Should-RunFeed $Phase $switched) {
    $results.Add((Run-Or-Inject 'linkedin-feed' $py @((Join-Path $root 'scripts\linkedin_feed.py')) 15 $true))
  }
}
Write-Status

if ($Phase -ne 'pull') {   # judge, liveness check, render and the queue guard: the processing steps. Since 2024-12-20 the
                           # trickle runs them too (suffix -trickle) so the LinkedIn rows are judged and shown while the laptop is awake
# The judge reads the day's CSV.

# Judge (2024-12-13): label each gate-passing row of today's CSV Apply / Maybe / Skip with quoted
# evidence, into dashboard\verdicts.db (scripts\judge.py, Sonnet 5.5, real time so the verdicts exist
# before the render and the 08:30 brief). Shadow mode until the calibration report clears kappa 0.6:
# the dashboard still shows the keyword band. No ANTHROPIC_API_KEY = the step skips itself cleanly.
# Paid, so test mode never runs it. 30 min: about 50 gate-passers at 10 to 20 s each.
if ($pythonOk) {
  $results.Add((Run-Or-Inject "judge$stepSuffix" $py @((Join-Path $root 'scripts\judge.py')) 30 $true))
  Write-Status
}

# Liveness-check the curated Apply roles against their ATS BEFORE rendering, so a role that has
# closed gets flagged "needs recheck" in the dashboard instead of silently showing as open (the
# loophole that let closed roles sit on Apply). Flags only, never deletes.
# A closed role makes the step degraded (it shows under "Recheck before applying"); the step fails
# only when the check itself could not run (2024-12-17). Runs before render so the flag is visible.
if ($pythonOk -and $Phase -ne 'late' -and $Phase -ne 'trickle') {
  $today = Get-Date -Format 'yyyy-MM-dd'
  $results.Add((Run-Or-Inject 'apply-check' $py @((Join-Path $root 'scripts\check_apply_liveness.py'),'--root',$root,'--date',$today) 5 $true))
}
Write-Status   # the render below reads this file for its health banner

# queue-check (2024-12-17): the Bright Data listing checks for the needs-validation queue, written into
# dashboard\queue-seen.json, as their own step with their own deadline. The render that follows reads
# those verdicts and fetches nothing, so a slow Bright Data night degrades this step and never the
# dashboard (render timed out at 300 s on 10/18 and ran 229 s on 10/19 doing these checks inline).
# Paid, so test mode skips it.
if ($pythonOk) {
  $results.Add((Run-Or-Inject "queue-check$stepSuffix" $py @((Join-Path $root 'scripts\job_dashboard.py'),'--queue-check-only') 10 $true))
  Write-Status
}
if ($pythonOk) {
  # The needs-validation queue is ranked from the raw, relevance-filtered feed (no JD text needed).
  # No network: queue verdicts come from queue-check, curated link states from apply-check.
  $dashArgs = @((Join-Path $root 'scripts\job_dashboard.py'),'--no-link-check','--no-queue-check')
  if (Is-Injected 'render') { $results.Add((Invoke-Injected "render$stepSuffix")) }
  else { $results.Add((Invoke-Step "render$stepSuffix" $py $dashArgs 5 $root)) }
  $processedAt = Get-Date -Format o   # today's rows are judged and rendered as of now
}

# Queue-health guard: an empty needs-validation queue is the silent failure that let the
# dashboard show stale data for weeks. If render succeeded but surfaced zero candidates across
# both tiers (needs-validation and Apply-now; Invoke-Step sums them), raise an alertable
# failure (reuses the SMTP alert below) so it can never rot unnoticed again.
if (-not $testMode) {
  $renderRec = $results | Where-Object { $_.Name -eq "render$stepSuffix" } | Select-Object -Last 1
  if ($renderRec -and $renderRec.ExitCode -eq 0 -and (-not $renderRec.RowCount)) {
    $results.Add((New-StepRecord 'queue-health' 3 $false 0 0 'needs-validation queue empty after render'))
  }
}
Write-Status
}   # end of the process-phase steps

$failed = @($results | Where-Object { $_.ExitCode -ne 0 })
# The alert is the status file: an email alert could never fire while the laptop was closed. The
# dashboard banner renders this record and the 09:30 cloud brief reads pipeline-status.json from the
# private brief repo, so one plain headline here is what both of them say.
$degradedSteps = @($results | Where-Object { $_.ExitCode -eq 0 -and $_.State -eq 'degraded' })
$degradedProviders = @()
foreach ($item in $results) {
  if (-not $item.Providers) { continue }
  foreach ($name in @($item.Providers.Keys)) {
    $prov = $item.Providers[$name]
    if ($prov['state'] -and $prov['state'] -ne 'ok') {
      $degradedProviders += ("{0}: {1}{2}" -f $name, $prov['state'], $(if ($prov['reason']) { " (" + $prov['reason'] + ")" } else { '' }))
    }
  }
}
$alertLevel = if ($failed.Count) { 'failed' } elseif ($degradedSteps.Count -or $degradedProviders.Count) { 'degraded' } else { 'ok' }
$runLabel = "${prefix}Night run $(Get-Date -Format 'yyyy-MM-dd')"
$alertHeadline = switch ($alertLevel) {
  'failed'   { "${runLabel}: $($failed.Count) step(s) failed ($(@($failed | ForEach-Object Name) -join ', '))" + $(if ($degradedProviders.Count) { "; degraded: " + ($degradedProviders -join '; ') } else { '' }) + '.' }
  'degraded' { "${runLabel}: every step ran; degraded: " + (@(@($degradedSteps | ForEach-Object Name) + $degradedProviders) -join '; ') + '.' }
  default    { "${runLabel}: all $($results.Count) steps clean." }
}
$alertRecord = @{
  status = 'status-file'; level = $alertLevel; headline = (Sanitize-Line $alertHeadline)
  failed_steps = @($failed | ForEach-Object Name); degraded_steps = @($degradedSteps | ForEach-Object Name)
  degraded_providers = @($degradedProviders | ForEach-Object { Sanitize-Line $_ })
  detail = 'Read by the dashboard banner and the cloud brief; the SMTP alert was retired 2024-12-17.'
}
Add-Content $monthLog "[$(Get-Date -Format o)] ALERT $alertLevel : $(Sanitize-Line $alertHeadline)"

# Nightly hygiene (2024-12-19): old merge locks and the leftover _filtered CSVs go; nothing else is pruned.
$pruned = Remove-SweepLeftovers $root $todayStamp
if ($pruned.Locks -or $pruned.Filtered) {
  Add-Content $monthLog "[$(Get-Date -Format o)] PRUNE weekly-sweeps: $($pruned.Locks) lock file(s), $($pruned.Filtered) _filtered CSV(s) removed"
}

# Final record (with the alert record), then one more render so the dashboard banner shows this
# run's complete result. The render makes no network calls, so this pass costs a few seconds and is
# not counted as a pipeline step.
Write-Status -Finished (Get-Date -Format o)
if ($pythonOk -and $Phase -ne 'pull' -and -not (Is-Injected 'render')) {
  $refresh = Invoke-Step 'render-refresh' $py $dashArgs 5 $root
  if ($refresh.ExitCode -ne 0) {
    Add-Content $monthLog "[$(Get-Date -Format o)] render-refresh failed exit=$($refresh.ExitCode): $($refresh.FirstErrorLine)"
  }
}

# Push tonight's inputs to the cloud brief's repo now (2024-12-12), so the 09:30 brief sees the run that
# just finished; the Cloud-Sync task (sign-in and 08:50 hourly) is the catch-up for days the laptop was
# off. Since 2024-12-20 the trickle syncs too, so the brief sees the LinkedIn rows. Not a pipeline step: a
# failure is logged, never a chain failure. Skipped in test mode so a test run never pushes to GitHub.
if ($pythonOk -and $Phase -ne 'pull' -and $env:JOBSEARCH_TEST_NO_PAID_SWEEPS -ne '1') {
  $sync = Invoke-Step 'cloud-sync' $py @((Join-Path $root 'scripts\cloud_sync.py')) 5 $root
  if ($sync.ExitCode -ne 0) {
    Add-Content $monthLog "[$(Get-Date -Format o)] cloud-sync failed exit=$($sync.ExitCode): $($sync.FirstErrorLine)"
  }
}

foreach ($item in $results) { Write-Host ("{0}: exit={1} timeout={2} rows={3}" -f $item.Name,$item.ExitCode,$item.TimedOut,$item.RowCount) }
Add-Content $monthLog "[$(Get-Date -Format o)] ${prefix}END failures=$($failed.Count)"
if ($failed.Count) { exit 1 }
exit 0
