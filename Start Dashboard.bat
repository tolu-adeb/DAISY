@echo off
REM Double-click: dashboard + API + live portfolio monitor.
cd /d "%~dp0"
if not exist ".venv\Scripts\abg.exe" (
  echo First-time setup: installing...
  python -m venv .venv
)
.venv\Scripts\python -c "import tzdata, plyer" 2>nul || .venv\Scripts\python -m pip install -e ".[all]"
start "" http://127.0.0.1:8000
.venv\Scripts\abg.exe serve
pause
