# 装箱算法接口对接说明

本文档面向系统接口对接人员，只说明算法的输入、输出和调用边界。

## 1. 调用方式

推荐系统侧直接传入标准化箱子列表，调用：

```python
from run_packing import build_workflow
from src.main.report_persister import NullReportPersister

workflow = build_workflow()
workflow._report_persister = NullReportPersister()

report = workflow.run_with_boxes(boxes)
```

说明：

- `boxes` 是系统传入的箱子列表。
- `run_with_boxes(boxes)` 返回内存中的 `report` 字典。
- 接口服务中建议使用 `NullReportPersister`，避免算法在接口调用时写本地 JSON/Excel 文件。
- 如果需要继续使用文件输出，可保留默认 `JsonFileReportPersister`。

## 2. 输入格式

接口入参建议为：

```json
{
  "boxes": [
    {
      "id": "BOX001",
      "type": "PKG-A",
      "length": 400,
      "width": 300,
      "height": 200,
      "weight": 12.5,
      "min_pack_multiple": 8,
      "pallet_type": "MH423C",
      "sales_order_no": "PAIN25450MN01S",
      "pallet_dims": {
        "length": 1440,
        "width": 2240,
        "height": 720
      }
    }
  ]
}
```

### 箱子字段

| 字段 | 类型 | 必填 | 说明 |
|---|---:|---:|---|
| `id` | string/int | 是 | 箱子唯一 ID。同一次请求中必须唯一 |
| `type` | string | 是 | 包装规格/箱型编码 |
| `length` | number | 是 | 箱子长度，单位 mm，必须大于 0 |
| `width` | number | 是 | 箱子宽度，单位 mm，必须大于 0 |
| `height` | number | 是 | 箱子高度，单位 mm，必须大于 0 |
| `weight` | number | 建议 | 重量。用于同尺寸箱子重箱在下规则；缺失时按 0 处理 |
| `min_pack_multiple` | number | 是 | 该箱子的指数值 |
| `pallet_type` | string | 是 | 托盘类型，用于匹配目标指数 |
| `sales_order_no` | string | 是 | 销售订单号。空值建议系统侧传 `UNKNOWN_ORDER` |
| `pallet_dims.length` | number | 是 | 托盘长度，单位 mm |
| `pallet_dims.width` | number | 是 | 托盘宽度，单位 mm |
| `pallet_dims.height` | number | 是 | 托盘高度，单位 mm |

### 输入约束

- 同一次调用中，`id` 不允许重复。
- `length / width / height / pallet_dims.*` 必须是正数。
- 同一个 `(pallet_type, sales_order_no)` 分组内应使用同一套 `pallet_dims`。
- `pallet_type` 必须能在算法配置中找到目标指数；否则该组托盘指数状态会是 `UNKNOWN`。
- 所有尺寸单位统一使用 `mm`。

## 3. 输出格式

`run_with_boxes()` 返回：

```json
{
  "packing_plan_id": null,
  "total_runtime_seconds": 123.45,
  "summary": {},
  "pallets": []
}
```

### 顶层字段

| 字段 | 类型 | 说明 |
|---|---:|---|
| `packing_plan_id` | string/null | 当前算法不生成业务计划号，默认 `null` |
| `total_runtime_seconds` | number | 算法总耗时，单位秒 |
| `summary` | object | 汇总统计 |
| `pallets` | array | 托盘明细列表 |

## 4. 汇总统计 `summary`

`summary` 包含：

| 字段 | 类型 | 说明 |
|---|---:|---|
| `overall` | object | 全部订单分组的汇总统计 |
| `by_pallet_type` | object | 按 `pallet_type + sales_order_no` 分组的统计 |

### `summary.overall` 常用字段

| 字段 | 类型 | 说明 |
|---|---:|---|
| `total_pallets` | number | 总托盘数 |
| `success_pallets` | number | 指数达标托盘数 |
| `failed_pallets` | number | 指数未达标托盘数 |
| `unknown_pallets` | number | 未配置目标指数的托盘数 |
| `avg_mpm_gap` | number | 未达标托盘平均指数缺口 |
| `max_mpm_gap` | number | 最大指数缺口 |
| `rescued_from_failed` | number | 失败托盘修复成功数量 |
| `runtime_breakdown_seconds` | object | 耗时拆解 |
| `kpi` | object | 救援和失败分层 KPI |

## 5. 托盘明细 `pallets[]`

每个托盘结构如下：

```json
{
  "pallet_id": "MH423C-PAIN25450MN01S-1",
  "pallet_type": "MH423C",
  "sales_order_no": "PAIN25450MN01S",
  "mpm_total": 196,
  "mpm_target": 192,
  "mpm_gap": -4,
  "mpm_status": "SUCCESS",
  "stability_checks": {
    "status": "SUCCESS"
  },
  "box_total_volume": 123456789,
  "pallet_volume": 2322432000,
  "fill_rate": 0.05316,
  "packed_items": []
}
```

### 托盘字段

| 字段 | 类型 | 说明 |
|---|---:|---|
| `pallet_id` | string | 算法生成的托盘 ID，格式通常为 `{pallet_type}-{sales_order_no}-{序号}` |
| `pallet_type` | string | 托盘类型 |
| `sales_order_no` | string | 销售订单号 |
| `mpm_total` | number | 当前托盘指数合计 |
| `mpm_target` | number/null | 当前托盘目标指数 |
| `mpm_gap` | number/null | `mpm_target - mpm_total` |
| `mpm_status` | string | `SUCCESS` / `FAILED` / `UNKNOWN` |
| `stability_checks.status` | string | 重心稳定状态，`SUCCESS` / `FAILED` |
| `box_total_volume` | number | 托盘内箱子总体积，单位 `mm^3` |
| `pallet_volume` | number | 托盘体积，单位 `mm^3` |
| `fill_rate` | number | 体积填充率，`box_total_volume / pallet_volume` |
| `packed_items` | array | 箱子摆放明细 |

## 6. 箱子摆放明细 `packed_items[]`

常用字段如下：

| 字段 | 类型 | 说明 |
|---|---:|---|
| `id` | string/int | 箱子 ID，对应输入 `boxes[].id` |
| `type` | string | 箱型编码 |
| `length` | number | 输出箱长，单位 mm |
| `width` | number | 输出箱宽，单位 mm |
| `height` | number | 输出箱高，单位 mm |
| `weight` | number | 重量 |
| `min_pack_multiple` | number | 箱子指数 |
| `position.x` | number | 箱子左下角 X 坐标，单位 mm |
| `position.y` | number | 箱子左下角 Y 坐标，单位 mm |
| `position.z` | number | 箱子底面 Z 坐标，单位 mm |
| `pallet_dims` | object | 所属托盘尺寸 |
| `supported_area` | number | 直接支撑面积 |
| `support_ratio` | number | 直接支撑率 |
| `suction_*` | mixed | 吸盘姿态和吸盘矩形信息，供机械臂侧使用 |

坐标约定：

- 原点为托盘底面左下角。
- `x` 沿托盘长度方向。
- `y` 沿托盘宽度方向。
- `z` 沿高度方向。
- 单位统一为 `mm`。

## 7. 状态字段约定

### `mpm_status`

| 值 | 含义 |
|---|---|
| `SUCCESS` | `mpm_total >= mpm_target` |
| `FAILED` | `mpm_total < mpm_target` |
| `UNKNOWN` | 未配置该 `pallet_type` 的目标指数 |

### `stability_checks.status`

| 值 | 含义 |
|---|---|
| `SUCCESS` | 重心稳定检查通过 |
| `FAILED` | 重心稳定检查失败，详情见 `stability_checks.center_of_mass_failure` |

## 8. 质量门禁

算法返回前会执行输出质量门禁。以下情况会抛出异常，不返回正常结果：

- 存在空托盘。
- 输入箱子未全部输出。
- 输出中出现输入之外的箱子。
- 同一个箱子重复出现在多个托盘中。
- 箱子尺寸或体积为 0。

接口层建议捕获异常，并返回统一错误结构，例如：

```json
{
  "success": false,
  "error_code": "PACKING_FAILED",
  "message": "输出质量门禁失败：missing_boxes=['BOX001']"
}
```

## 9. 文件输出

如果使用默认 `JsonFileReportPersister`，算法会在 `output/` 目录生成：

| 文件 | 说明 |
|---|---|
| `packing_plan_<timestamp>.json` | 完整装箱结果 |
| `packing_plan_summary_<timestamp>.xlsx` | 托盘级统计 Excel |

Excel 字段：

- `托盘ID`
- `托盘尺寸(mm)`
- `箱子数量`
- `稳定性状态`
- `指数`
- `目标指数`
- `指数缺口`
- `指数状态`

接口服务通常不建议写本地文件，应使用 `NullReportPersister` 后直接返回 `report`。

## 10. 对接建议

- 系统侧在调用前校验必填字段和正数尺寸，避免把脏数据传入算法。
- 系统侧保留输入箱子 ID 到业务对象 ID 的映射，算法输出用 `packed_items[].id` 回填业务结果。
- 接口响应建议直接返回 `summary.overall`、`pallets`，其它诊断字段可作为调试字段保留。
- 如果接口需要异步执行，建议用任务 ID 管理运行状态，因为真实数据单次运行可能持续数分钟。
