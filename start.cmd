@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating project environment...
    py -3.11 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi,sklearn,prometheus_client,httpx,uvicorn" >nul 2>nul
if errorlevel 1 (
    echo Installing pinned dependencies...
    set PIP_NO_INDEX=0
    ".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
    if errorlevel 1 goto failed
)
echo Starting MLOps dashboard at http://127.0.0.1:8765
echo Keep this window open. Press Ctrl+C to stop the service.
".venv\Scripts\python.exe" -m src --demo --open-browser
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Please read the error above and docs\runbook.md.
pause
exit /b 1
