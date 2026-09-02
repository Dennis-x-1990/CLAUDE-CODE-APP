@echo off
setlocal
cd /d "%~dp0"

REM ── Claude Code WebUI launcher ──────────────────────────────────────
REM Installs deps on first run, starts the server if not already up,
REM then opens the browser. Safe to run repeatedly.

python -c "import fastapi, uvicorn, multipart, pydantic" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -q -r requirements.txt python-multipart pywinpty
)

netstat -ano | findstr /C:":9020" | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 (
  echo Starting Claude Code WebUI server...
  start "ClaudeWebUI" /MIN cmd /c "python app.py > webui-server.log 2>&1"
  timeout /t 3 /nobreak >nul
)

start "" http://127.0.0.1:9020
echo Claude Code WebUI: http://127.0.0.1:9020
