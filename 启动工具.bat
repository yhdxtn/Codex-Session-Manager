@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "APP_DIR=%~dp0"

if exist "%APP_DIR%dist-v1.1.11\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.11\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.9\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.9\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.8\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.8\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.7\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.7\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.6\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.6\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.5\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.5\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.4\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.4\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.3\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.3\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.2\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.2\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist-v1.1.1\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist-v1.1.1\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

if exist "%APP_DIR%dist\CodexHistoryViewer\CodexHistoryViewer.exe" (
  start "" "%APP_DIR%dist\CodexHistoryViewer\CodexHistoryViewer.exe"
  exit /b 0
)

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found. Install Python or use the bundled EXE.
  pause
  exit /b 1
)

if not exist "%APP_DIR%.venv\Scripts\python.exe" (
  py -3 -m venv "%APP_DIR%.venv"
  if errorlevel 1 goto setup_failed
)

"%APP_DIR%.venv\Scripts\python.exe" -c "import PySide6, zstandard" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages. Network access is needed for this first setup.
  "%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements.txt"
  if errorlevel 1 goto setup_failed
)

start "" "%APP_DIR%.venv\Scripts\pythonw.exe" "%APP_DIR%main.py"
exit /b 0

:setup_failed
echo Setup failed. Check Python and network access, then retry or read README.md.
pause
exit /b 1

