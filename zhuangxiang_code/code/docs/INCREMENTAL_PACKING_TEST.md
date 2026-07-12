# 增量订单装箱测试说明

本文档说明当前独立增量装箱测试层。该层不修改现有装箱算法，只在算法外部编排两次装箱和结果合并。

## 业务流程

1. 第一次调用算法，装箱初始订单。
2. 从第一次结果中保留 `mpm_status == SUCCESS` 的托盘。
3. 提取第一次结果中未达标托盘上的箱子。
4. 将这些未达标托盘箱子与新增箱合并。
5. 第二次调用算法，只重算这部分箱子。
6. 合并“第一次达标托盘”和“第二次装箱结果”。
7. 做输出质量门禁：不漏箱、不重复箱、不输出空托盘。

## 代码位置

| 文件 | 作用 |
|---|---|
| `src/incremental/loader.py` | 读取三表 Excel，拆分初始箱和新增箱 |
| `src/incremental/service.py` | 执行两阶段增量装箱和结果合并 |
| `run_incremental_packing.py` | 批量运行 5 个测试文件并输出报告 |
| `tests/test_incremental.py` | 增量合并语义回归测试 |

## Excel 适配

测试文件包含：

- `最终挑选结果`
- `新增箱`
- `包装物料主数据(BMS)`

其中 `6000/7000` 文件第一张表是 5000 个初始箱；`8000/9000/10000` 文件第一张表是累计箱。loader 会自动兼容：

- 如果第一张表为 5000 行，直接作为初始订单。
- 如果第一张表大于 5000 行，则用“第一张表 - 新增箱”还原初始 5000 箱。

## 运行方式

在仓库根目录执行：

```bash
python code/run_incremental_packing.py
```

也可以指定文件：

```bash
python code/run_incremental_packing.py data/selected_8000_boxes_9_1_concentrated_3orders.xlsx
```

## 输出文件

默认输出到 `output/incremental/`：

| 文件 | 说明 |
|---|---|
| `*_incremental_report.json` | 每个测试文件的完整增量装箱结果 |
| `incremental_test_summary.xlsx` | 5 个测试文件的汇总表 |
| `incremental_test_summary.csv` | 汇总表 CSV |
| `cache/initial_*.json` | 初始 5000 箱装箱结果缓存 |

## 汇总字段

| 字段 | 说明 |
|---|---|
| `initial_boxes` | 初始订单箱数 |
| `new_boxes` | 新增订单箱数 |
| `initial_repack_boxes` | 第一次未达标托盘中进入第二次重算的旧箱数 |
| `total_pallets` | 合并后的总托盘数 |
| `success_pallets` | 合并后的指数达标托盘数 |
| `failed_pallets` | 合并后的指数未达标托盘数 |
| `avg_mpm_gap` | 未达标托盘平均指数缺口 |
| `max_mpm_gap` | 最大指数缺口 |
| `runtime_seconds` | 本次增量阶段耗时。复用历史 JSON 时取报告中的记录 |

## 当前测试结果

| 文件 | 初始箱 | 新增箱 | 重算旧箱 | 总托盘 | 达标 | 未达标 |
|---|---:|---:|---:|---:|---:|---:|
| `selected_6000_boxes_9_1_concentrated_3orders.xlsx` | 5000 | 1000 | 1524 | 237 | 131 | 106 |
| `selected_7000_boxes_9_1_concentrated_3orders.xlsx` | 5000 | 2000 | 1524 | 270 | 167 | 103 |
| `selected_8000_boxes_9_1_concentrated_3orders.xlsx` | 5000 | 3000 | 1524 | 302 | 186 | 116 |
| `selected_9000_boxes_9_1_concentrated_3orders.xlsx` | 5000 | 4000 | 1524 | 335 | 194 | 141 |
| `selected_10000_boxes_9_1_concentrated_3orders.xlsx` | 5000 | 5000 | 1524 | 387 | 217 | 170 |

质量检查结果：

- 5 个 JSON 报告均无空托盘。
- 5 个 JSON 报告均无重复箱。
- 5 个 JSON 报告均满足箱子守恒。
