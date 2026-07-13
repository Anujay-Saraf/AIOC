@echo off
setlocal
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -ApiPort 8000 -WebPort 3000
endlocal
