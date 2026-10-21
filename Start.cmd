@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\bootstrap.ps1"
if errorlevel 1 pause
endlocal
