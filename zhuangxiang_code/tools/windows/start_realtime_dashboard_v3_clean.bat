@echo off
setlocal

set PROJECT_DIR=%~dp0..\..
for %%I in ("%PROJECT_DIR%") do set PROJECT_DIR=%%~fI

set WORKSPACE_DIR=%PROJECT_DIR%\..
for %%I in ("%WORKSPACE_DIR%") do set WORKSPACE_DIR=%%~fI

rem ?? Python venv
set VENV_PY=%WORKSPACE_DIR%\.venvs\packing-realtime\Scripts\python.exe

rem Conda prefix ??
if not exist "%VENV_PY%" (
    set VENV_PY=%WORKSPACE_DIR%\.venvs\packing-realtime\python.exe
)

if not exist "%VENV_PY%" (
    echo [ERROR] Project Python not found:
    echo %WORKSPACE_DIR%\.venvs\packing-realtime
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
"%VENV_PY%" "%PROJECT_DIR%\apps\realtime_dashboard\realtime_dashboard_v3_clean.py"

if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error.
    pause
)

endlocal
