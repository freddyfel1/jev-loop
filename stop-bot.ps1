# One-click stop for the "Stop Trading Bot" desktop shortcut. Asks the
# running paper loop to stop cleanly through data\stop.request: the loop
# cancels its resting orders and exits, exactly as Ctrl+C in its window
# does. Leaves the dashboard server running. Shows a popup with the result.
$project = $PSScriptRoot
$data = Join-Path $project 'data'
$stopFile = Join-Path $data 'stop.request'
$startLog = Join-Path $data 'start-bot.log'
$popup = New-Object -ComObject WScript.Shell

function Write-StartLog($msg) {
    Add-Content -Path $startLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

function Get-Loop {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'jevloop' -and $_.CommandLine -match 'run --paper' }
}

Write-StartLog 'stop shortcut clicked'
if (-not (Get-Loop)) {
    Write-StartLog 'no loop running'
    $popup.Popup('The trading bot is not running.', 0, 'Stop Trading Bot', 64) | Out-Null
    exit 0
}

New-Item -ItemType File -Force -Path $stopFile | Out-Null
# The loop checks once per tick (2s); give it time to cancel orders and exit.
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    if (-not (Get-Loop)) {
        Write-StartLog 'loop stopped cleanly'
        $popup.Popup('Trading bot stopped. Its open orders were cancelled.', 0, 'Stop Trading Bot', 64) | Out-Null
        exit 0
    }
}

Remove-Item -Force -ErrorAction SilentlyContinue $stopFile
Write-StartLog 'loop did not stop within 30s'
$popup.Popup(
    "The trading bot did not stop within 30 seconds. It may be a run started before this shortcut existed: stop it with Ctrl+C in its 'jev-loop' window.",
    0, 'Stop Trading Bot', 48
) | Out-Null
exit 1
