@echo off
setlocal
cd /d "%~dp0"
set UV_PROJECT_ENVIRONMENT=.venv-dev
uv run lab-control-demo
if errorlevel 1 pause
