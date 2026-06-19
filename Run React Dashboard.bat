@echo off
setlocal

cd /d "%~dp0"

echo Starting Trades Dashboard API...
start "Trades Dashboard API" cmd /k "python -m uvicorn api:app --host 127.0.0.1 --port 8000"

echo Starting React dashboard...
start "Trades Dashboard React" cmd /k "npm run dev -- --host 127.0.0.1 --port 5173"

echo Opening dashboard...
timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:5173/"

endlocal
