# 工业装箱工作台 V2 使用说明

## 1. 这版界面的设计目标

这版不再按“参数/表格/指标”平均堆满界面，而是改成更友好的工业工作台：

```text
顶部：任务状态 + 常用操作
左侧：流程引导和参数
中间：3D托盘可视化 / 箱子列表 / 稳定性分析
右侧：当前托盘摘要 + 操作建议 + 风险与异常
底部：后端运行日志
```

适合你的使用流程：

```text
选择配置 → 开始装箱 → 看后端日志 → 自动加载结果 → 看3D箱垛和稳定性 → 导出托盘分析
```

## 2. 新增文件

把补丁包解压到：

```text
E:\research_code\zhuang-ui\zhuangxiang_code
```

解压后会新增：

```text
apps\realtime_dashboard\realtime_dashboard_v2.py
tools\windows\start_realtime_dashboard_v2.bat
docs\README_工业装箱工作台V2.md
```

不会覆盖你原来的 `stability_business_dashboard_json.py` 和旧版 `realtime_dashboard_runner.py`。

## 3. 运行方式

在 VSCode 终端中进入：

```powershell
Set-Location -LiteralPath "E:\research_code\zhuang-ui\zhuangxiang_code"
```

推荐直接用虚拟环境 Python 运行：

```powershell
& "E:\research_code\zhuang-ui\.venvs\packing-realtime\Scripts\python.exe" ".\apps\realtime_dashboard\realtime_dashboard_v2.py"
```

或者运行 bat：

```powershell
& ".\tools\windows\start_realtime_dashboard_v2.bat"
```

## 4. 注意

这版依赖你当前已经修好的 `stability_business_dashboard_json.py`。如果它再次报 `f-string unmatched`，说明旧文件被覆盖回错误版本，需要重新打之前那处 `pressure_utilization` 补丁。

