@echo off
rem Opens the jev-loop dashboard. Starts the dashboard server (via jev.ps1, so it
rem reads .\data) if nothing is on port 8765. Dashboard only: never starts trading.
cd /d "%~dp0"
netstat -ano | findstr /r /c:"127.0.0.1:8765 .*LISTENING" >nul
if errorlevel 1 (
    start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0jev.ps1" serve --port 8765
    timeout /t 3 /nobreak >nul
)
start "" http://127.0.0.1:8765/index.html
