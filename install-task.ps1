# Registers the Windows scheduled task that runs the feeder every 5 minutes for the current user (no console window).
# Since 2026-10-16 the dashboard server (server.js) runs the feeder itself every 5 minutes, so this task is mainly the
# watchdog that restarts the server after a reboot, log-in or crash (the feeder starts server.js when it is down).
# Settings: runs on battery and keeps running when the laptop unplugs (the old task skipped every run on battery),
# starts a missed run as soon as possible after sleep, and never runs two copies at once.
#   powershell -ExecutionPolicy Bypass -File install-task.ps1            install or update
#   powershell -ExecutionPolicy Bypass -File install-task.ps1 -Remove    remove
param([switch]$Remove)
$name = "Cluster dashboard feeder"
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; exit $LASTEXITCODE }
$vbs = Join-Path $PSScriptRoot "run-feeder.vbs"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
# also at every log-on, so after a reboot the dashboard server and the feeder start as soon as you sign in (not at the next 5-minute tick)
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
try {
  Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon, $trigger) -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
  Write-Host "Scheduled: '$name' at log-on and every 5 minutes (on battery too) -> $vbs"
} catch {
  Write-Host "Log-on trigger refused ($($_.Exception.Message)); registering the 5-minute trigger only"
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  if ($?) { Write-Host "Scheduled: '$name' every 5 minutes (on battery too) -> $vbs" }
}
