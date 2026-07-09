# -*- coding: utf-8 -*-
$ErrorActionPreference = "Stop"

Write-Host "========== Packing Realtime Environment Setup ==========" -ForegroundColor Cyan

# 当前脚本位置：
# E:\research_code\装箱算法_h\zhuangxiang_code\tools\setup
$SetupDir = $PSScriptRoot
$ToolsDir = Split-Path -Parent $SetupDir
$ProjectDir = Split-Path -Parent $ToolsDir
$WorkspaceDir = Split-Path -Parent $ProjectDir

$VenvDir = Join-Path $WorkspaceDir ".venvs\packing-realtime"
$RuntimeDir = Join-Path $WorkspaceDir "runtime\packing-realtime"
$LogsDir = Join-Path $RuntimeDir "logs"
$TempDir = Join-Path $RuntimeDir "temp"
$ExportsDir = Join-Path $RuntimeDir "exports"

$ReqFile = Join-Path $ProjectDir "apps\realtime_dashboard\requirements_realtime.txt"

Write-Host "ProjectDir   = $ProjectDir" -ForegroundColor Gray
Write-Host "WorkspaceDir = $WorkspaceDir" -ForegroundColor Gray
Write-Host "VenvDir      = $VenvDir" -ForegroundColor Gray
Write-Host "ReqFile      = $ReqFile" -ForegroundColor Gray

# 创建运行目录
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
New-Item -ItemType Directory -Force -Path $ExportsDir | Out-Null

# 检查 requirements
if (!(Test-Path -LiteralPath $ReqFile)) {
    throw "找不到 requirements 文件：$ReqFile"
}

# 找 Python
$PythonCmd = $null

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    $PythonCmd = "py"
} else {
    $pythonNormal = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonNormal) {
        $PythonCmd = "python"
    }
}

if ($null -eq $PythonCmd) {
    throw "没有找到 Python。请先安装 Python 3.10 或 3.11，并确保 python 或 py 命令可用。"
}

# 创建虚拟环境
if (!(Test-Path -LiteralPath $VenvDir)) {
    Write-Host "正在创建虚拟环境..." -ForegroundColor Yellow

    if ($PythonCmd -eq "py") {
        & py -3 -m venv $VenvDir
    } else {
        & python -m venv $VenvDir
    }
} else {
    Write-Host "虚拟环境已存在，跳过创建。" -ForegroundColor Green
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (!(Test-Path -LiteralPath $VenvPython)) {
    throw "虚拟环境创建失败，找不到：$VenvPython"
}

Write-Host "正在升级 pip..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip

Write-Host "正在安装实时看板依赖..." -ForegroundColor Yellow
& $VenvPython -m pip install -r $ReqFile

Write-Host ""
Write-Host "========== 环境配置完成 ==========" -ForegroundColor Green
Write-Host "虚拟环境位置：" -ForegroundColor Green
Write-Host $VenvDir -ForegroundColor Green
Write-Host ""
Write-Host "下一步在 VSCode 终端执行：" -ForegroundColor Cyan
Write-Host "& `"$VenvDir\Scripts\Activate.ps1`"" -ForegroundColor Cyan
Write-Host "python `"$ProjectDir\apps\realtime_dashboard\realtime_dashboard_runner.py`"" -ForegroundColor Cyan