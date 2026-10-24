@echo off
setlocal EnableExtensions
title AICTE WorkflowOS Launcher
cd /d "%~dp0"

echo.
echo ==============================================
echo        AICTE WorkflowOS - Local Launcher
echo ==============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python was not found. Install Python 3.11+ and enable Add Python to PATH.
  pause
  exit /b 1
)
where node >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js was not found. Install the Node.js LTS release.
  pause
  exit /b 1
)

if not exist "backend\.venv\Scripts\python.exe" (
  echo [1/5] Creating Python virtual environment...
  python -m venv "backend\.venv"
  if errorlevel 1 goto :fail
)

echo [2/5] Installing backend dependencies...
"backend\.venv\Scripts\python.exe" -m pip install -r "backend\requirements.txt"
if errorlevel 1 goto :fail

echo [3/5] Checking frontend dependencies...
pushd "frontend"
call npm install
if errorlevel 1 (
  popd
  goto :fail
)
popd

echo [4/5] Starting backend...
start "AICTE WorkflowOS - Backend" "%~dp0backend\run_backend.bat"
if errorlevel 1 goto :fail

timeout /t 2 /nobreak >nul
echo [5/5] Starting frontend...
start "AICTE WorkflowOS - Frontend" "%~dp0frontend\run_frontend.bat"
if errorlevel 1 goto :fail

timeout /t 4 /nobreak >nul
start "" "http://localhost:5173"
echo.
echo Launcher started.
echo Frontend: http://localhost:5173
echo Backend API docs: http://127.0.0.1:8000/docs
echo Close the backend/frontend windows to stop the app.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup or launch failed. Read the messages above and fix the reported issue.
pause
exit /b 1
