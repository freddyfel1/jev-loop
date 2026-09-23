# One-click start for the desktop shortcut: makes sure the dashboard server
# and the paper-trading loop are running, then opens the dashboard.
# Safe to click again: anything already running is left alone. Paper only;
# this never passes --live. Stop the loop with Ctrl+C in its "jev-loop" window
# (that cancels resting orders).
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$launcher = Join-Path $project 'jev.ps1'
$dashboard = 'http://127.0.0.1:8765/index.html'

function Get-JevProcess($pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'jevloop' -and $_.CommandLine -match $pattern }
}

if (-not (Get-JevProcess 'serve')) {
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launcher, 'serve', '--port', '8765'
    )
}

if (-not (Get-JevProcess 'run --paper')) {
    $log = Join-Path $project 'data\continuous.log'
    $loopCmd = "`$host.UI.RawUI.WindowTitle = 'jev-loop (paper) - Ctrl+C to stop cleanly'; " +
        "`$env:PYTHONUNBUFFERED = '1'; Set-Location '$project'; " +
        "& '$launcher' run --paper --forever 2>&1 | Tee-Object -FilePath '$log'"
    Start-Process powershell.exe -WindowStyle Minimized -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $loopCmd
    )
}

# Give the server a moment to come up before opening the page.
foreach ($i in 1..20) {
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8765/latest.json" -TimeoutSec 1 | Out-Null; break }
    catch { Start-Sleep -Milliseconds 500 }
}
Start-Process $dashboard
