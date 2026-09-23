# Windows launcher for jev-loop: the PowerShell equivalent of `set -a; source .env; set +a`.
# Loads .env into this process, keeps all run-time data in .\data (JEV_LOOP_HOME), points
# Python's requests at .\data\ca-bundle.pem (certifi + the Norton Web/Mail Shield root,
# needed because Norton re-signs HTTPS on this PC), then runs `python -m jevloop` with
# whatever arguments you pass.
#   .\jev.ps1 run --paper --ticks 30 --symbol BTC/USD
#   .\jev.ps1 serve
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$envFile = Join-Path $project '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            if ($value) { Set-Item -Path "Env:$($Matches[1])" -Value $value }
        }
    }
}
$env:JEV_LOOP_HOME = Join-Path $project 'data'
$bundle = Join-Path $env:JEV_LOOP_HOME 'ca-bundle.pem'
if (Test-Path $bundle) { $env:REQUESTS_CA_BUNDLE = $bundle }
& (Join-Path $project '.venv\Scripts\python.exe') -m jevloop @args
exit $LASTEXITCODE
