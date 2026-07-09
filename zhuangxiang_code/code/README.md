# 装箱算法项目（code）

`code/` 是当前装箱算法项目目录。项目目标是在满足业务规则、几何约束、机械约束和箱子守恒的前提下，尽量提高达到目标指数的托盘数量。

项目现在支持两种运行模式：

- **一次性装箱**：一次传入完整订单箱子，输出完整装箱方案。
- **增量装箱测试**：先装初始订单；后续有“新增箱”时，把首次未达标托盘上的旧箱和新增箱合并后再次装箱，再与首次达标托盘合并。

## 核心目标

- 按 `(pallet_type, sales_order_no)` 分组装箱。
- 每个托盘尽量达到对应托盘类型的目标指数。
- 箱子充足且仍可合法放入时，优先继续装满当前托盘。
- 所有箱子最终都必须进入托盘；不允许漏箱、重箱、空托盘输出。
- 增量订单场景下，不改核心算法，只在算法外部做两阶段编排和结果合并。

## 主装箱算法：GCP（默认）

默认主算法是 **GCP（全局列式装箱 + 柱级组合优化）**：把同底面箱子凑成「柱」、再用
组合优化（Set-Partitioning ILP / CP-SAT，支持 90° 旋转）最大化每组的达标托盘数，对
规则订单达标率显著优于旧 beam（668 单订单 2 → 11）。每个分组**自适应判定**：规则
成分高走 GCP，复杂/不规则分组自动回退下面的「配方优先 baseline」，保证不退步。
配置 `main_packer: gcp`（默认）| `beam`（全程走 baseline）。
详见 [`docs/全局列式装箱算法说明.md`](docs/全局列式装箱算法说明.md)。

下文「配方优先」「箱子旋转」「装箱约束」对两条路径都适用（GCP 回退时即走配方优先 baseline）。

## 配方优先装箱（recipe-first，GCP 的回退路径）

每个分组在主装箱前先做"配方配额规划"，解决贪心装箱把稀缺"伴层箱"过量烧进早期托盘、导致后续大箱凑不出目标指数的问题：

1. 跑基线 `pack_group`（现有完整管线，行为不变）。
2. `src/main/recipe_planner.py` 按全组库存枚举"整层 + 顶带"配方（Σ层高 ≤ 托盘高、Σ指数 ≥ 目标），在库存约束下最大化达标实例数。
3. 每个配方实例用真实装箱器实装，必须"整池装入 + 达标 + 整盘门禁"；装不出的实例箱回落剩余池。
4. 剩余箱再跑一遍 `pack_group` 兜底。
5. **严格棘轮**：仅当配方方案达标托盘严格更多、且全组箱子守恒时才采用，否则原样返回基线方案——达标数结构上只增不减。

5000 箱实测：达标 89 → 141（46.6% → 76.6%），总托盘 191 → 184，端到端约 281 秒。

### 运行模式

- **fast（默认）**：直接采用"配方实例 + 兜底"方案，不再额外跑一遍全量基线。配方路径不可用（规划不出实例 / 实装全失败 / 守恒失败）时自动回退基线装箱，行为与旧版一致。
- **safe（`--safe`）**：恢复配方/基线双跑棘轮对比，仅当配方方案达标严格更多且守恒时才采用。用于定期审计或首次处理新类型订单数据：

```bash
python code/run_packing.py --safe
```

两种模式下，6mm 间隙、支撑率、吸盘可达、重心等全部硬约束门禁与箱子守恒校验完全一致。

### 装箱形态

配方实例由整层格栅装箱器实装：自托盘底面 z=0 起一层层向上码放；每层从托盘角点 (0,0) 开始、沿 x 方向排满一行再换下一行，行优先铺满；大箱/重箱在下，顶带整行贴排。形态规整，便于机器人与人工执行。

## 箱子旋转（朝向规整）

箱子允许 90° 旋转，两条路径各自处理：

- **GCP**：落地时每柱选最优朝向，CP-SAT 摆柱支持旋转；自适应判定 `suits_group` 用
  `_orient_per`（真实几何根数）替代 265 网格量化，**非模数箱型也能正确走 GCP**。
- **baseline**：入口对每个箱子做**朝向规整**（开关 `allow_box_rotation_90`，默认 `true`），
  带几何门槛——仅对「旋转能让该箱型单类整层从不达标跨到达标」的箱型换向，对「旋转
  也救不了的难组」保持原朝向、不打乱已紧凑布局（保填充率、不增盘数）。

修复了固定朝向对旋转敏感箱型（如 530×350、740×330）系统性装不满的缺陷：真实 BMS 中
约 40% 箱型旋转敏感，构造的旋转敏感订单 baseline 达标 0 → 全救回，668/5000 零回归。
详见 [`docs/箱子旋转泛化性评估与修复.md`](docs/箱子旋转泛化性评估与修复.md)。

## 业务规则

| 规则 | 说明 | 位置 |
|---|---|---|
| 分组 | 按 `(pallet_type, sales_order_no)` 分组，组内独立装箱 | `src/main/order_processor.py` |
| 指数目标 | `MH423C -> 192`，`MH110 -> 32` | `src/config/constants.py` |
| 达标判定 | 根据 `mpm_total` 与托盘目标指数判定 `SUCCESS / FAILED` | `src/rescue/pallet_evaluator.py` |
| 标准输入 | 支持直接传入标准化 `boxes` | `src/main/workflow.py` |
| 普通 Excel 输入 | 两表 Excel 适配为标准化 `boxes` | `src/data/excel_loader.py` |
| 新增箱输入 | 三表 Excel 拆成初始箱和新增箱 | `src/incremental/loader.py` |
| 箱子守恒 | 输出前校验不漏箱、不重箱、不空托盘 | `src/main/result_formatter.py` |
| 当前托盘装满优先 | 达标后继续尝试吸收可合法放入的剩余箱 | `src/main/pallet_packer.py` |

## 装箱约束

所有约束通过 `code/config/packing_config.yaml` 的 `constraints` 段统一配置（单一事实来源 `src/config/constraint_config.py`）。**必须约束**永远生效、只有数值可调；**可关约束**默认开启、可配置关闭。放置时拦截与最终门禁读同一份配置，保证同源。详见 [`docs/约束配置与扩展指南.md`](docs/约束配置与扩展指南.md)。

### 必须约束（不可关，部分数值可配）

| 约束 | 说明 | 可配项（默认） | 位置 |
|---|---|---|---|
| 不超界 | 箱子不得超出托盘 `length / width / height` | —— | `src/packing/placement_validator.py` |
| 不重叠 | 箱子之间不得发生几何重叠 | —— | `src/geometry/overlap.py`、`src/packing/placement_validator.py` |
| 箱间间隙 | 相邻箱子在 XY 方向的正向间隙必须小于阈值 | `max_box_gap_mm`（`6.0`） | `src/geometry/gap_checker.py` |
| 支撑率 | 非底层箱子的直接支撑率必须不低于阈值 | `support_ratio_threshold`（`0.8`） | `src/geometry/support.py` |
| 重心稳定 | 整体重心相对托盘中心的偏移比例不得超过阈值 | `center_of_mass_tolerance`（`1/3`） | `src/geometry/center_of_mass.py` |

### 可关约束（默认开启，可配置关闭）

| 约束 | 说明 | 开关（默认 `true`） | 位置 |
|---|---|---|---|
| 吸盘可达 | 吸盘垂直下放路径不得被遮挡；吸盘尺寸 `suction_cup_length/width` 等可配 | `suction_reachability_enabled` | `src/packing/suction_planner.py` |
| 小箱在下(不压大箱) | 小箱(`is_small_box`)正下方不得有体积更大的箱子，防较重小箱压坏大箱 | `small_box_below_enabled` | `src/utils/helpers.py`、`src/geometry/constraint_validator.py` |
| 同尺寸重箱在下 | 同尺寸箱子上下叠放时，重箱必须在下、轻箱必须在上 | `same_size_heavier_below_enabled` | `src/packing/stacking_policy.py` |
| 按倍数凑层 | 同 footprint、不同高度且存在整数倍关系时，优先同层堆叠（软偏好，只影响搜索顺序） | `height_multiple_layering_enabled` | `src/packing/stacking_policy.py`、`src/packing/layer_pool_builder.py` |

> `constraints` 段还有装箱朝向开关 `allow_box_rotation_90`（默认 `true`）——它不是约束、
> 不拦截放置，而是 baseline 的箱子旋转能力开关，详见上文「箱子旋转（朝向规整）」。

### 约束配置与运行

```bash
python run_packing.py                          # 默认读 config/packing_config.yaml
python run_packing.py --config my_config.yaml  # 指定自定义配置
```

改约束值/开关只需编辑 YAML，无需改代码；甲方新增约束或替换装箱算法的步骤见 [`docs/约束配置与扩展指南.md`](docs/约束配置与扩展指南.md)。排序偏好只影响搜索顺序，不替代几何可行性校验。

## 一次性装箱

### 输入

可以使用普通 Excel 文件，也可以直接调用：

```python
workflow.run_with_boxes(boxes)
```

标准化箱子至少包含：

- `id`
- `type`
- `length`
- `width`
- `height`
- `weight`
- `pallet_type`
- `sales_order_no`
- `min_pack_multiple`
- `pallet_dims`

### 运行

在仓库根目录执行：

```bash
python code/run_packing.py
```

默认输出：

- `output/packing_plan_<timestamp>.json`
- `output/packing_plan_summary_<timestamp>.xlsx`

## 新增箱/增量装箱测试

真实接口场景中，订单可能不是一次性全部到达。当前新增了一个**独立增量装箱测试层**，用于验证这种场景，不修改现有装箱算法。

### 业务流程

1. 第一次调用算法，装箱初始订单。
2. 保留第一次结果中 `mpm_status == SUCCESS` 的托盘。
3. 提取第一次结果中未达标托盘上的箱子。
4. 将这些旧箱与“新增箱”合并。
5. 第二次调用算法，只重算“未达标旧箱 + 新增箱”。
6. 合并“第一次达标托盘”和“第二次装箱结果”。
7. 执行输出质量门禁：不漏箱、不重箱、不输出空托盘。

### 实现位置

| 文件 | 作用 |
|---|---|
| `src/incremental/loader.py` | 读取三表 Excel，拆分初始箱和新增箱 |
| `src/incremental/service.py` | 两阶段增量装箱和结果合并 |
| `src/incremental/__init__.py` | 增量模块导出入口 |
| `run_incremental_packing.py` | 批量运行 5 个新增箱测试文件 |
| `tests/test_incremental.py` | 增量合并语义回归测试 |

### 三表 Excel 结构

新增箱测试文件包含：

- `最终挑选结果`
- `新增箱`
- `包装物料主数据(BMS)`

适配规则：

- `6000/7000` 文件第一张表是 5000 个初始箱，直接作为初始订单。
- `8000/9000/10000` 文件第一张表是累计箱；loader 会用“最终挑选结果 - 新增箱”还原初始 5000 箱。
- `新增箱` 表作为第二次到达的订单箱。
- `包装物料主数据(BMS)` 提供 `min_pack_multiple` 等指数信息。

### 运行

运行全部 5 个新增箱测试文件：

```bash
python code/run_incremental_packing.py
```

运行指定文件：

```bash
python code/run_incremental_packing.py data/selected_8000_boxes_9_1_concentrated_3orders.xlsx
```

### 输出

默认输出到 `output/incremental/`：

| 文件 | 说明 |
|---|---|
| `*_incremental_report.json` | 每个测试文件的完整增量装箱结果 |
| `incremental_test_summary.xlsx` | 5 个测试文件汇总表 |
| `incremental_test_summary.csv` | 汇总表 CSV |
| `cache/initial_*.json` | 初始 5000 箱装箱结果缓存 |

脚本支持断点续跑：

- 已存在的 `*_incremental_report.json` 会直接复用。
- 初始 5000 箱方案会写入 `output/incremental/cache/`，后续文件可复用。

### 汇总字段

| 字段 | 说明 |
|---|---|
| `initial_boxes` | 初始订单箱数 |
| `new_boxes` | 新增订单箱数 |
| `initial_repack_boxes` | 第一次未达标托盘中进入第二次重算的旧箱数 |
| `initial_failed_pallets` | 第一次装箱未达标的托盘数 |
| `initial_failed_recovered_pallets` | 救回托盘数：第二次装箱结果中"达标且至少含一个首跑未达标箱子"的托盘数（未达标托盘会被拆散重组，按此口径衡量"原未达标托盘有多少在增量后达标"） |
| `initial_failed_boxes_in_success` | 首跑未达标箱子中，最终落在达标托盘上的箱数 |
| `total_pallets` | 合并后的总托盘数 |
| `success_pallets` | 合并后的指数达标托盘数 |
| `failed_pallets` | 合并后的指数未达标托盘数 |
| `avg_mpm_gap` | 未达标托盘平均指数缺口 |
| `max_mpm_gap` | 最大指数缺口 |
| `runtime_seconds` | 增量阶段耗时；复用历史 JSON 时取报告记录 |

### 当前 5 个文件测试结果

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

更详细说明见：

- `docs/INCREMENTAL_PACKING_TEST.md`
- `docs/API_INTEGRATION.md`

## 目录结构

```text
code/
├─ run_packing.py
├─ run_incremental_packing.py
├─ config.yaml
├─ README.md
├─ docs/
├─ src/
│  ├─ config/
│  ├─ data/
│  ├─ geometry/
│  ├─ incremental/
│  │  ├─ __init__.py
│  │  ├─ loader.py
│  │  └─ service.py
│  ├─ main/
│  ├─ packing/
│  ├─ rescue/
│  └─ utils/
└─ tests/
   ├─ test_incremental.py
   ├─ test_main.py
   ├─ test_packing.py
   └─ test_rescue_pool.py
```

## 关键模块

| 模块 | 作用 |
|---|---|
| `src/main/workflow.py` | 端到端工作流，支持 `run()` 和 `run_with_boxes()`；逐组 `suits_group` 分流 GCP / baseline |
| `src/packing/global_column_packer.py` | GCP 主算法：凑柱 + 柱级组合优化（ILP/CP-SAT，支持旋转、去模数化判定） |
| `src/main/recipe_planner.py` | 配方配额规划：整层+顶带配方枚举、库存约束下最大化达标实例数 |
| `src/main/recipe_first.py` | 配方优先编排：基线/配方双方案对比，达标严格更多且守恒才采用 |
| `src/main/pallet_packer.py` | 单分组主装箱流程、当前托盘补箱、失败托盘修复编排 |
| `src/main/result_formatter.py` | 汇总统计、完整报告构造、输出质量门禁 |
| `src/main/report_persister.py` | JSON 和 Excel 汇总持久化 |
| `src/packing/direct_layer_packer.py` | 整层确定性装箱与单箱居中兜底 |
| `src/packing/beam_search_packer.py` | 通用 beam search 装箱 |
| `src/packing/stacking_policy.py` | 重箱在下、倍数组层等共享堆叠策略 |
| `src/incremental/loader.py` | 新增箱测试 Excel 适配 |
| `src/incremental/service.py` | 增量订单两阶段编排 |

## 主要配置

| 配置 | 文件 |
|---|---|
| **装箱约束（间隙/支撑率/重心/各开关/吸盘几何）** | **`config/packing_config.yaml`、`src/config/constraint_config.py`** |
| 托盘目标指数 | `src/config/constants.py` |
| 小箱阈值检测参数 | `src/config/constants.py` |
| 托盘几何尺寸 | `src/config/pallet_config.py` |
| beam width / restart / candidate_limit | `src/config/algorithm_config.py` |
| 输出持久化方式 | `src/main/report_persister.py` |

约束配置的完整说明与扩展方法见 [`docs/约束配置与扩展指南.md`](docs/约束配置与扩展指南.md)。

## 测试

核心回归测试：

```bash
python code/tests/test_packing.py
python code/tests/test_main.py
python code/tests/test_rescue_pool.py
python code/tests/test_incremental.py
python code/tests/test_recipe_planner.py
python code/tests/test_incremental_gate.py
python code/tests/test_small_box_constraint.py
python code/tests/test_constraint_config.py
```

如环境中已安装 `pytest`，也可以统一运行测试集。
