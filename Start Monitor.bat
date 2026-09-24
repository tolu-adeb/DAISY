@echo off
REM Double-click: headless live monitor (signals to desktop / Discord / email). Ctrl+C to stop.
cd /d "%~dp0"
if not exist ".venv\Scripts\abg.exe" (
  echo First-time setup: installing...
  python -m venv .venv
)
.venv\Scripts\python -c "import tzdata, plyer" 2>nul || .venv\Scripts\python -m pip install -e ".[all]"
title ABG live monitor
.venv\Scripts\abg.exe monitor
pause
