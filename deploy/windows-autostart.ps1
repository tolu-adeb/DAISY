# Keep the live monitor (and the MNQ bot inside it) running on this Windows PC.
#
# Why: in Oct 2026 the bot was running at the 9:30 open on only ~5 of 15 sessions (started late or not at all),
# so most signals never happened.  This registers a Task Scheduler task that starts "Start Monitor.bat"
#   * when you log on, and
#   * every weekday at 9:00 (wakes the PC if it is asleep and plugged in),
# and restarts it if it crashes.  monitor.lock stops a second copy if one is already running.
#
# Run once from PowerShell in the repo folder:
#     powershell -ExecutionPolicy Bypass -File deploy\windows-autostart.ps1
# Remove later with:
#     Unregister-ScheduledTask -TaskName "ABG Monitor" -Confirm:$false
#
# A server (docs/13) is still the better home: a laptop that is closed, asleep on battery or off will miss the open.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repo "Start Monitor.bat"
if (-not (Test-Path $bat)) { throw "Start Monitor.bat not found in $repo" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start `"ABG live monitor`" /min `"$bat`"" -WorkingDirectory $repo
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 9:00am)
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun `
    -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "ABG Monitor" -Action $action -Trigger $triggers -Settings $settings `
    -Description "ABG Intelligence Terminal live monitor + MNQ signal bot (docs/14)" -Force | Out-Null

Write-Host "Registered 'ABG Monitor': starts at logon and weekdays 9:00 (local time - set for US Eastern)."
Write-Host "Check it in Task Scheduler, or run: Get-ScheduledTask -TaskName 'ABG Monitor' | Get-ScheduledTaskInfo"
