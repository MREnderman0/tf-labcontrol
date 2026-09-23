@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-dev\Scripts\lab-control.exe" powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-dev.ps1
if errorlevel 1 exit /b 1
".venv-dev\Scripts\lab-control.exe" %*
if errorlevel 1 pause
