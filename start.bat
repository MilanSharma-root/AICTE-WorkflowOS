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
  python -m venv backend\.venv
  if errorlevel 1 goto :fail
)
echo [2/5] Installing backend dependencies...
call backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if errorlevel 1 goto :fail
echo [3/5] Installing frontend dependencies...
if not exist "frontend\node_modules" (
  pushd frontend
  call npm install
  if errorlevel 1 (popd & goto :fail)
  popd
)
echo.
echo [4/5] Starting FastAPI backend in a new terminal...
start "AICTE WorkflowOS - Backend" cmd /k "cd /d ""%~dp0backend"" && ""%~dp0backend\.venv\Scripts\python.exe"" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
timeout /t 2 /nobreak >nul
echo [5/5] Starting Vite frontend in a new terminal...
start "AICTE WorkflowOS - Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev -- --host 127.0.0.1"
timeout /t 3 /nobreak >nul
start "" "http://localhost:5173"
echo.
echo Launcher commands sent. SQLite is used by default; PostgreSQL can be configured in backend\.env.
echo Frontend: http://localhost:5173
echo Backend API docs: http://127.0.0.1:8000/docs
echo.
pause
exit /b 0
:fail
echo.
echo [ERROR] Setup failed. Read the messages above, fix the reported issue, and run start.bat again.
pause
exit /b 1
