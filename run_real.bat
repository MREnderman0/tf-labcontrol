@echo off
setlocal
cd /d "%~dp0"
set UV_PROJECT_ENVIRONMENT=.venv-dev
uv run --locked lab-control %*
if errorlevel 1 pause
