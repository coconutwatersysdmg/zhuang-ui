@echo off
chcp 65001 >nul
setlocal EnableExtensions

set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..\..") do set PROJECT_DIR=%%~fI
for %%I in ("%PROJECT_DIR%\..") do set WORKSPACE_DIR=%%~fI

set VENV_DIR=%WORKSPACE_DIR%\.venvs\packing-realtime
set PY_EXE=%VENV_DIR%\Scripts\python.exe
set APP_ENTRY=%PROJECT_DIR%\apps\realtime_dashboard\realtime_dashboard_v2.py
set LOG_DIR=%WORKSPACE_DIR%\runtime\packing-realtime\logs

if not exist "%PY_EXE%" (
    echo [ERROR] Python venv not found:
    echo   %PY_EXE%
    echo.
    echo Please create/install the environment first.
    pause
    exit /b 1
)

if not exist "%APP_ENTRY%" (
    echo [ERROR] App entry not found:
    echo   %APP_ENTRY%
    pause
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
cd /d "%PROJECT_DIR%"
echo [INFO] Project: %PROJECT_DIR%
echo [INFO] Python : %PY_EXE%
echo [INFO] Launching Industrial Packing Workbench V2...
"%PY_EXE%" "%APP_ENTRY%" --project "%PROJECT_DIR%"

pause
