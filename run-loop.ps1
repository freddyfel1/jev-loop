# The window the paper loop runs in, started (minimized) by start-bot.ps1.
# Tees the loop's output to data\continuous.log and records how the loop
# ended in data\start-bot.log, so a run that dies at once is never silent.
# Stop it with the "Stop Trading Bot" shortcut or Ctrl+C here.
$host.UI.RawUI.WindowTitle = 'jev-loop (paper) - Ctrl+C to stop cleanly'
$env:PYTHONUNBUFFERED = '1'
$project = $PSScriptRoot
Set-Location $project
$data = Join-Path $project 'data'
$startLog = Join-Path $data 'start-bot.log'

function Write-StartLog($msg) {
    Add-Content -Path $startLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

Write-StartLog 'loop window started'
try {
    & (Join-Path $project 'jev.ps1') run --paper --forever 2>&1 |
        ForEach-Object { "$_" } |
        Tee-Object -FilePath (Join-Path $data 'continuous.log')
    Write-StartLog "loop exited (code $LASTEXITCODE)"
}
catch {
    Write-StartLog "loop window error: $_"
    Write-Host "`nThe loop failed to start: $_`nThis window closes in 60 seconds."
    Start-Sleep -Seconds 60
}
