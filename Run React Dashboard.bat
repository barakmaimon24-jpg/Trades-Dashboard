@echo off
setlocal

cd /d "%~dp0"

curl.exe -fsS --max-time 2 http://127.0.0.1:8000/api/health >nul 2>nul
if errorlevel 1 (
  echo Starting Trades Dashboard API...
  start "Trades Dashboard API" cmd /k "python -m uvicorn api:app --host 127.0.0.1 --port 8000"
) else (
  echo Trades Dashboard API is already running.
)

echo Waiting for API...
for /l %%i in (1,1,30) do (
  curl.exe -fsS --max-time 2 http://127.0.0.1:8000/api/health >nul 2>nul
  if not errorlevel 1 goto api_ready
  timeout /t 1 /nobreak >nul
)
echo API did not become ready on http://127.0.0.1:8000
pause
exit /b 1

:api_ready

curl.exe -fsS --max-time 2 http://127.0.0.1:5173/ >nul 2>nul
if errorlevel 1 (
  echo Starting React dashboard...
  start "Trades Dashboard React" cmd /k "npm run dev -- --host 127.0.0.1 --port 5173"
) else (
  echo React dashboard is already running.
)

echo Waiting for React dashboard...
for /l %%i in (1,1,30) do (
  curl.exe -fsS --max-time 2 http://127.0.0.1:5173/ >nul 2>nul
  if not errorlevel 1 goto site_ready
  timeout /t 1 /nobreak >nul
)
echo React dashboard did not become ready on http://127.0.0.1:5173
pause
exit /b 1

:site_ready

echo Opening dashboard...
start "" "http://127.0.0.1:5173/"

endlocal
