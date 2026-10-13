# Registers the Windows scheduled task that runs the feeder every 5 minutes for the current user (no console window).
# The feeder also restarts the dashboard server when it is not running, so the dashboard survives reboots and log-ins.
#   powershell -ExecutionPolicy Bypass -File install-task.ps1            install or update
#   powershell -ExecutionPolicy Bypass -File install-task.ps1 -Remove    remove
param([switch]$Remove)
$name = "Cluster dashboard feeder"
if ($Remove) { schtasks /Delete /TN $name /F; exit $LASTEXITCODE }
$vbs = Join-Path $PSScriptRoot "run-feeder.vbs"
schtasks /Create /TN $name /SC MINUTE /MO 5 /TR "wscript.exe `"$vbs`"" /F
if ($LASTEXITCODE -eq 0) { Write-Host "Scheduled: '$name' every 5 minutes -> $vbs" }
