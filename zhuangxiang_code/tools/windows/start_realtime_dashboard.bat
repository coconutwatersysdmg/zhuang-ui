@echo off
chcp 65001 >nul
setlocal EnableExtensions

set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..\..") do set PROJECT_DIR=%%~fI
for %%I in ("%PROJECT_DIR%\..") do set WORKSPACE_DIR=%%~fI

set VENV_DIR=%WORKSPACE_DIR%\.venvs\packing-realtime
set PY_EXE=%VENV_DIR%\Scripts\python.exe
set APP_ENTRY=%PROJECT_DIR%\apps\realtime_dashboard\realtime_dashboard_runner.py
set LOG_DIR=%WORKSPACE_DIR%\runtime\packing-realtime\logs

if not exist "%PY_EXE%" (
    echo [错误] 找不到实时装箱 UI 虚拟环境：
    echo   %VENV_DIR%
    echo.
    echo 请先在 PowerShell 运行：
    echo   powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\tools\setup\setup_packing_realtime_env.ps1"
    echo.
    pause
    exit /b 1
)

if not exist "%APP_ENTRY%" (
    echo [错误] 找不到实时看板入口：
    echo   %APP_ENTRY%
    pause
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%PROJECT_DIR%"
echo [INFO] 源码目录：%PROJECT_DIR%
echo [INFO] 虚拟环境：%VENV_DIR%
echo [INFO] 启动实时装箱看板...
"%PY_EXE%" "%APP_ENTRY%" --project "%PROJECT_DIR%"

pause
