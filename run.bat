@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-dev\Scripts\python.exe" goto setup
".venv-dev\Scripts\python.exe" -c "import lab_control, PySide6.QtWidgets, pyvisa, serial, numpy, pyqtgraph" >nul 2>&1
if errorlevel 1 goto setup
:run
".venv-dev\Scripts\python.exe" -m lab_control %*
set "launch_result=%errorlevel%"
if not "%launch_result%"=="0" pause
exit /b %launch_result%
:setup
echo Preparing the project environment using the configured domestic mirror...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-dev.ps1
if errorlevel 1 exit /b 1
goto run
