# One-click start for the desktop shortcut: makes sure the dashboard server
# and the paper-trading loop are running, then opens the dashboard.
# Safe to click again: anything already running is left alone. Paper only;
# this never passes --live. Stop the loop with Ctrl+C in its "jev-loop" window
# (that cancels resting orders).
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$launcher = Join-Path $project 'jev.ps1'
$dashboard = 'http://127.0.0.1:8765/index.html'
$startLog = Join-Path $project 'data\start-bot.log'

function Write-StartLog($msg) {
    Add-Content -Path $startLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}
trap { Write-StartLog "ERROR: $_"; break }
Write-StartLog 'shortcut clicked'

function Get-JevProcess($pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'jevloop' -and $_.CommandLine -match $pattern }
}

if (Get-JevProcess 'serve') { Write-StartLog 'dashboard server already running' }
else {
    Write-StartLog 'starting dashboard server'
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launcher, 'serve', '--port', '8765'
    )
}

if (Get-JevProcess 'run --paper') { Write-StartLog 'loop already running' }
else {
    Write-StartLog 'starting paper loop'
    Start-Process powershell.exe -WindowStyle Minimized -WorkingDirectory $project -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $project 'run-loop.ps1')`""
    )
}

# Give the server a moment to come up before opening the page.
foreach ($i in 1..20) {
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8765/latest.json" -TimeoutSec 1 | Out-Null; break }
    catch { Start-Sleep -Milliseconds 500 }
}
Start-Process $dashboard
Write-StartLog 'dashboard opened'
