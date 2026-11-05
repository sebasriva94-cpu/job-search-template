# Registers the Job-Search automation under Task Scheduler folder \JobSearch\.
# Idempotent: unregisters existing ones first. Run as the normal user (no admin needed).
$ErrorActionPreference = 'Stop'
$dir   = $PSScriptRoot
$psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$folder = "\JobSearch\"

$user     = "$env:USERDOMAIN\$env:USERNAME"
function Settings([int]$Minutes, [bool]$StartWhenAvailable = $true) {
  # 2024-12-20: RestartCount only ever covered launch failures (a chain that exits 1 is never relaunched), and a
  # real restart would be a second paid chain, so it is 0. StartWhenAvailable is off for the Night and Trickle
  # tasks: the laptop is mostly off, and a power-on would otherwise start their missed instances next to the
  # catch-up's; the catch-up alone makes a missed night up.
  $settings = New-ScheduledTaskSettingsSet -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $Minutes) -RestartCount 0
  $settings.StartWhenAvailable = $StartWhenAvailable
  return $settings
}

# $scriptName = bare .ps1 filename; $scriptArgs = args passed to the script (kept
# OUTSIDE the -File path so PowerShell doesn't treat "script.ps1 -Reason x" as one path).
# $trigger may be one trigger or an array of triggers.
function Reg($name, $trigger, $scriptName, $scriptArgs = "", $settings = $null) {
  if (-not $settings) { $settings = Settings 90 }
  $full = "$folder$name"
  Unregister-ScheduledTask -TaskName $name -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue
  # 2024-12-23: the host runs hidden. The tasks run in the signed-in session (the unlock trigger needs that), so
  # the chain's powershell.exe had a console window, and closing it killed the run (exit 0xC000013A). The steps
  # were already hidden.
  $argline = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\$scriptName`""
  if ($scriptArgs) { $argline += " $scriptArgs" }
  $action = New-ScheduledTaskAction -Execute $psExe -Argument $argline
  Register-ScheduledTask -TaskName $name -TaskPath $folder -Action $action `
    -Trigger $trigger -Settings $settings -User $user -RunLevel Limited | Out-Null
  Write-Host "registered $full"
}

# 1. The night chain (-Phase full), 00:30 LOCAL time. A trigger keeps its absolute
#    offset if the machine changes time zone and then fires an hour off; re-registering here pins it to 00:30 in whatever zone the laptop is in, which is
#    what linkedin_pull.py's NIGHT_WINDOW assumes. The fast lane (everything but LinkedIn) takes
#    minutes; LinkedIn has its own task below. It must run after midnight because the CSV and every
#    later step are keyed to the calendar date.
Reg "Pull-LinkedIn-Jobs-Night" (New-ScheduledTaskTrigger -Daily -At 12:30AM) "pull-jobs.ps1" "-Phase full" (Settings 90 $false)

# 1a. The LinkedIn lane (-Phase trickle) at 01:00, after the fast lane has rendered: 16 groups 12-20
#     minutes apart through the night, escalating 429 pauses, posting text from the public job page.
#     6-hour limit. Since 2024-12-20 the phase ends with judge, queue-check, render and sync (steps
#     suffixed -trickle), so its rows reach the dashboard and the brief while the laptop is still awake.
#     7-hour limit (2024-12-20: the guest lane alone may take 300 min and the processing steps up to 55 more).
Reg "Pull-LinkedIn-Jobs-Trickle" (New-ScheduledTaskTrigger -Daily -At 1:00AM) "pull-jobs.ps1" "-Phase trickle" (Settings 420 $false)
Unregister-ScheduledTask -TaskName "Pull-LinkedIn-Jobs" -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue

# 1b. (Retired 2024-12-20) The 07:30 Late task. It never fired on a morning the laptop slept, and the
#     hourly catch-up now runs the same late pass whenever today's CSV is newer than the status file's
#     processed stamp (Resolve-Phase in pull-jobs.lib.ps1). Do not re-add it.
Unregister-ScheduledTask -TaskName "Pull-LinkedIn-Jobs-Late" -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue

# 1c. Catch-up (-Phase catchup): at sign-in and every hour from 07:00 to 22:00. Exits in under a
#     second when today's run is under way or finished with nothing new; runs the late pass when rows
#     were merged after the last render (2024-12-20); otherwise runs the full chain. This is the
#     real guarantee after a missed night: WakeToRun did not wake the laptop on 2024-12-17 and
#     StartWhenAvailable did not catch the missed instance up either.
#     2024-12-23: sixteen plain daily triggers, one per hour, instead of one 07:00 trigger repeating hourly
#     for 16 h. With the laptop off at 07:00, StartWhenAvailable fired the missed start once and no repetition
#     followed, so a killed chain was never retried. A daily trigger whose time was missed fires once at wake
#     and the others fire on their own hour; MultipleInstances IgnoreNew drops the burst at wake.
function New-HourlyDailyTriggers([int]$FromHour, [int]$ToHour) {
  @($FromHour..$ToHour | ForEach-Object { New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($_)) })
}
# 2024-12-20: the laptop is mostly off, so the moments it is known to be awake are the primary triggers: sign-in,
# workstation unlock (session state change 8) and resume from sleep (Kernel-Power event 107), each after a
# two-minute delay for the network. New-ScheduledTaskTrigger cannot express the last two; they are CIM instances.
$ns = 'Root/Microsoft/Windows/TaskScheduler'
$unlock = New-CimInstance -CimClass (Get-CimClass -Namespace $ns -ClassName MSFT_TaskSessionStateChangeTrigger) -ClientOnly `
  -Property @{ StateChange = 8; UserId = $user; Enabled = $true; Delay = 'PT2M' }
$resumeQuery = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Kernel-Power''] and EventID=107]]</Select></Query></QueryList>'
$resume = New-CimInstance -CimClass (Get-CimClass -Namespace $ns -ClassName MSFT_TaskEventTrigger) -ClientOnly `
  -Property @{ Subscription = $resumeQuery; Enabled = $true; Delay = 'PT2M' }
$catchup = @(
  (New-ScheduledTaskTrigger -AtLogOn -User $user),
  $unlock,
  $resume
) + (New-HourlyDailyTriggers 7 22)
Reg "Pull-LinkedIn-Jobs-Catchup" $catchup "pull-jobs.ps1" "-Phase catchup" (Settings 360)

# 2. Sync of the brief's inputs to the private private brief repo (cloud_sync.py): \JobSearch\Cloud-Sync, registered
#    by hand on 2024-12-09 (venv pythonw.exe) and since 2024-12-19 triggered at sign-in and 08:50 hourly for 15 h.
#    Left as is here. The chain also runs the sync as its last step, so the brief sees the run that just finished.

# (Removed 2024-12-10) Open-Claude-Morning (08:05, launch-claude.ps1) is retired. It existed only
# so the Claude Desktop routines could fire: the laptop daily brief moved to a cloud routine
# (reads the private brief repo) and the Monday weekly review was retired on
# 2024-12-10, its weekly numbers (market snapshot, stage counts, write-off suggestions) now being
# the Monday edition of that same cloud brief. Nothing on the laptop needs the app open any more.
# (Removed 2024-08-30) No Sunday Open-Claude task either. Do not re-add either of them.
Unregister-ScheduledTask -TaskName "Open-Claude-Morning" -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "`n=== \JobSearch\ tasks ==="
Get-ScheduledTask -TaskPath $folder | Select-Object TaskName,State | Format-Table -Auto
