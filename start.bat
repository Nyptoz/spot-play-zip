@echo off
REM spotpl - one Spotify link in, one ZIP out.
REM Double-click this file on Windows. First run sets everything up, later runs just start.
cd /d "%~dp0"
title spotpl

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo Python was not found. Install Python 3.10+ from https://python.org and tick "Add to PATH".
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating environment - this happens once...
  python -m venv .venv
  if errorlevel 1 goto :failed
)

echo [2/3] Installing spotdl - this happens once...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto :failed

echo [3/3] Starting spotpl - your browser will open...
echo.
".venv\Scripts\python.exe" spotpl.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Setup failed. Scroll up for the error.
pause
exit /b 1
