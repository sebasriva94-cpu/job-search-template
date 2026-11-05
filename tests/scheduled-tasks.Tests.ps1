# Pester 3.x. Run from the Job Search root:
#   powershell -NoProfile -Command "Invoke-Pester -Path tests\scheduled-tasks.Tests.ps1"
# Checks the live \JobSearch\ tasks that register-tasks.ps1 registers (2024-12-19: the catch-up's hourly
# trigger was a -Once trigger dated 10/20 and had no next run after that day).

$folder = '\JobSearch\'

Describe 'Pull-LinkedIn-Jobs-Catchup trigger' {
  $task = Get-ScheduledTask -TaskPath $folder -TaskName 'Pull-LinkedIn-Jobs-Catchup' -ErrorAction SilentlyContinue
  It 'is registered' {
    ($null -ne $task) | Should Be $true
  }
  if ($task) {
    $daily = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' })
    $logon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    It 'has sixteen daily triggers, one per hour from 07:00 to 22:00, and one sign-in trigger (2024-12-23)' {
      $daily.Count | Should Be 16
      $logon.Count | Should Be 1
      $hours = @($daily | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') } | Sort-Object)
      ($hours -join ' ') | Should Be ((7..22 | ForEach-Object { '{0:00}:00' -f $_ }) -join ' ')
    }
    It 'no daily trigger repeats: a missed 07:00 cannot silence the later hours' {
      foreach ($d in $daily) { [string]$d.Repetition.Interval | Should Be '' }
    }
    It 'has a next run time' {
      $info = Get-ScheduledTaskInfo -TaskPath $folder -TaskName 'Pull-LinkedIn-Jobs-Catchup'
      ($null -ne $info.NextRunTime) | Should Be $true
    }
  }
}

Describe 'Catch-up fires when the laptop is known to be awake (2024-12-20)' {
  $task = Get-ScheduledTask -TaskPath $folder -TaskName 'Pull-LinkedIn-Jobs-Catchup' -ErrorAction SilentlyContinue
  if ($task) {
    It 'has a workstation-unlock trigger (session state change 8) and a resume trigger (Kernel-Power 107)' {
      $unlock = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskSessionStateChangeTrigger' })
      $unlock.Count | Should Be 1
      $unlock[0].StateChange | Should Be 8
      $event = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskEventTrigger' })
      $event.Count | Should Be 1
      $event[0].Subscription | Should Match 'Kernel-Power'
      $event[0].Subscription | Should Match 'EventID=107'
    }
  }
}

Describe 'Night and Trickle do not start missed instances; nothing restarts a chain' {
  foreach ($name in 'Pull-LinkedIn-Jobs-Night', 'Pull-LinkedIn-Jobs-Trickle') {
    $t = Get-ScheduledTask -TaskPath $folder -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
      It "$name has StartWhenAvailable off and RestartCount 0" {
        $t.Settings.StartWhenAvailable | Should Be $false
        $t.Settings.RestartCount | Should Be 0
      }
    }
  }
  $c = Get-ScheduledTask -TaskPath $folder -TaskName 'Pull-LinkedIn-Jobs-Catchup' -ErrorAction SilentlyContinue
  if ($c) {
    It 'Catchup keeps StartWhenAvailable on' {
      $c.Settings.StartWhenAvailable | Should Be $true
    }
  }
}

Describe 'The chain hosts run hidden (2024-12-23)' {
  foreach ($name in 'Pull-LinkedIn-Jobs-Night', 'Pull-LinkedIn-Jobs-Trickle', 'Pull-LinkedIn-Jobs-Catchup') {
    $t = Get-ScheduledTask -TaskPath $folder -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
      It "$name starts powershell.exe with -WindowStyle Hidden, so no console window can be closed by hand" {
        $t.Actions[0].Arguments | Should Match '-WindowStyle Hidden'
      }
    }
  }
}

Describe 'Pull-LinkedIn-Jobs-Late is retired (2024-12-20)' {
  It 'is not registered; the hourly catch-up runs the late pass when rows arrived after the last render' {
    $late = Get-ScheduledTask -TaskPath $folder -TaskName 'Pull-LinkedIn-Jobs-Late' -ErrorAction SilentlyContinue
    ($null -eq $late) | Should Be $true
  }
}
