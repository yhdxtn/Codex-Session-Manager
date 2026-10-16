@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "APP_DIR=%~dp0"

if not exist "%APP_DIR%.venv\Scripts\python.exe" (
  py -3 -m venv "%APP_DIR%.venv"
  if errorlevel 1 goto build_failed
)

"%APP_DIR%.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements-build.txt"
if errorlevel 1 goto build_failed

for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.base_prefix)"') do set "PYTHON_BASE=%%I"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PYTHON_BASE%;%APP_DIR%.venv\Scripts"

pushd "%APP_DIR%"
"%APP_DIR%.venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --windowed --name CodexHistoryViewer --distpath "%APP_DIR%dist-v1.1.11" --workpath "%APP_DIR%build-v1.1.11" --specpath "%APP_DIR%build-spec-v1.1.11" main.py
set "BUILD_EXIT=%ERRORLEVEL%"
popd
if not "%BUILD_EXIT%"=="0" goto build_failed
echo Build complete: %APP_DIR%dist-v1.1.11\CodexHistoryViewer\CodexHistoryViewer.exe
pause
exit /b 0

:build_failed
echo EXE build failed. Review the errors above.
pause
exit /b 1

