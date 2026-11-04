@echo off
REM ---------------------------------------------------------------
REM  University Timetable Agent - double-click launcher (Windows)
REM  First run: creates .venv, installs everything, loads the workbook.
REM  Every run: starts the web app and opens your browser.
REM ---------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 ( echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1 )
  echo Installing (first run takes a few minutes)...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -e ".[gemini,claude,dev]"
  if errorlevel 1 ( echo Installation failed. & pause & exit /b 1 )
)
if not exist "data\processed\dataset.json" (
  echo Loading the timetable workbook...
  ".venv\Scripts\ttagent.exe" ingest
)
echo Starting Timetable Agent at http://127.0.0.1:8000  (close this window to stop)
".venv\Scripts\ttagent.exe" serve
pause
