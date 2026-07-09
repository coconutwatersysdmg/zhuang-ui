@echo off
setlocal
set PROJECT_DIR=%~dp0..\..
for %%I in ("%PROJECT_DIR%") do set PROJECT_DIR=%%~fI
set WORKSPACE_DIR=%PROJECT_DIR%\..
for %%I in ("%WORKSPACE_DIR%") do set WORKSPACE_DIR=%%~fI
set VENV_PY=%WORKSPACE_DIR%\.venvs\packing-realtime\Scripts\python.exe

if not exist "%VENV_PY%" (
  echo [ERROR] Python venv not found: %VENV_PY%
  echo Please create the packing-realtime environment first.
  pause
  exit /b 1
)

cd /d "%PROJECT_DIR%"
"%VENV_PY%" "%PROJECT_DIR%\apps\realtime_dashboard\realtime_dashboard_v3_clean.py"
endlocal
