# case_group 同组约束：语义、实现与接入接口

> 2026-07-06 实装于当前代码。全部内容与当前代码一致（`src/utils/case_group.py` 等），
> 无未实现的假设。

## 1. 业务语义

箱子可带一个**可选属性 `case_group`**：

- 值为 **0**（或缺失/空/None/NaN）＝ **无约束**，行为与历史完全一致；
- 值**非 0** ＝ 该箱**只能与相同 `case_group` 值的箱子拼到同一个托盘**上，
  同时仍须满足所有既有装箱约束（间隙/支撑/吸盘/重心/小箱在下等）。

推论（校验口径）：一个合法托盘上所有箱子的归一化 `case_group` 必须完全相同——
要么全 0（无约束盘），要么全为同一个非 0 值。任何混装（含 0 与非 0 混）都违规。

注意：非 0 组的箱子**不会**与无约束（0）箱子拼盘。若某组箱子凑不满目标指数，
该组托盘可能因此更难达标——这是约束本身的代价，不是算法缺陷。

## 2. 取值归一化（三种来源口径一致）

`normalize_case_group`（`src/utils/case_group.py`）：

| 输入 | 归一化结果 |
|---|---|
| `0` / `0.0` / `'0'` / `''` / `None` / NaN（Excel 空单元格）/ 缺失 | `0`（无约束） |
| `1` / `1.0` / `'1'` / `'1.0'` | `'1'`（数值统一为整数字符串） |
| `' A-2 '` | `'A-2'`（字符串去首尾空白） |

即：Excel 浮点列、JSON 数字、系统接口字符串传同一个组值，都会归到同一组。

## 3. 传入接口（三种，属性名统一为 `case_group`）

### 3.1 系统接口（正式接入，预留）

公司系统通过接口传箱子时，在每个箱子对象上加同名属性即可——算法侧**无需再改代码**：

```python
boxes = [
    {
        "id": "BOX001", "type": "PKG-A",
        "length": 350, "width": 265, "height": 240, "weight": 5,
        "min_pack_multiple": 2, "pallet_type": "MH423C",
        "sales_order_no": "SO123",
        "pallet_dims": {"length": 1440, "width": 2240, "height": 720},
        "case_group": 3,          # ← 新属性；0/缺失＝无约束
    },
    ...
]
report = workflow.run_with_boxes(boxes)
```

待甲方接口文档到位后，只需在**接口适配层**把其字段映射为箱子字典的
`case_group` 键（名字相同则直接透传），无算法侧改动。

### 3.2 普通 Excel（本地测试）

任务表（默认 sheet `最终挑选结果`）加**可选列 `case_group`**：

| 箱子序号 | Box类型 | … | case_group |
|---|---|---|---|
| B1 | T1 | … | 1 |
| B2 | T1 | … | （空＝无约束） |

- **缺列或空单元格 ＝ 0 ＝ 无约束**（现有 Excel 全部兼容，行为不变）。
- 读取位置：`src/data/excel_loader.py`。

### 3.3 增量三表 Excel

`最终挑选结果` / `新增箱` 两张箱子表同样支持可选列 `case_group`
（`src/incremental/loader.py`）。增量重排阶段约束自动保持（重排也按分组隔离）。

## 4. 实现方式（为什么可靠）

**分组隔离（结构性保证）＋ 双层门禁（保险）**：

1. **分组隔离**：`OrderProcessor.group_by_order` 对非 0 组在销售订单号键上追加
   内部后缀（`__CASEGROUP__<值>`，复用与组内子聚类 `__SPLITREST__` 相同的成熟
   机制），使不同 `case_group` 的箱子**落入不同分组**。整条流水线（GCP / baseline
   / 配方 / 全部救援链 / 增量重排）都以分组为边界、从不跨组混盘 → 约束天然满足。
   输出前 `workflow._restore_split_orders` 剥离后缀：对外 `sales_order_no` 是真实
   订单号、盘号统一重编不重复，同时把 `case_group` 写回托盘级字段。
2. **整盘门禁**：`validate_pallet_constraints` 增加 `case_group_mixed` 检查
   （`src/geometry/constraint_validator.py`），任何单盘混装直接判违规。
3. **输出门禁（权威）**：`ResultFormatter.validate_output_quality` 用**输入箱
   id→case_group 映射**逐盘校验纯度（不依赖输出字段透传），违规抛
   `ValueError: ... case_group_mixed_pallets=[...]`，结果不出门。

数据不带 `case_group`（全 0）时：分组键与历史**逐字节一致**、两层门禁永不触发
——已实测 668 数据集达标/总盘 (10, 12) 与改动前逐位一致（零回归）。

## 5. 输出中的体现

- **托盘级**：非 0 组的托盘带 `"case_group": "<值>"` 字段（0 组托盘无此字段）。
- **箱子级**：`packed_items` 中每个箱子保留其 `case_group` 字段（输出为 deepcopy
  透传）。
- `sales_order_no` / `pallet_id` 均为真实值，内部后缀不外泄。

## 6. 验证与回归

```bash
python code/tests/test_case_group.py     # 本约束专项（7 项）
python code/tests/test_main.py           # 分组/工作流回归
python code/tests/test_incremental.py    # 增量回归
```

专项覆盖：归一化口径、标签往返、分组隔离与零回归键、整盘门禁、输出门禁
（字段剥离也能抓到）、端到端（一单混 3 组 → 3 个纯盘全达标、守恒、后缀还原、
盘号唯一）、Excel 带列/缺列。
