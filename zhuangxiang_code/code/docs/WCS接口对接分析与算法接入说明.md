# WCS 接口对接分析与算法接入说明

> 依据：《北自柳州五菱项目接口文档》v1.5（2026-04-11，WCS(北自) ↔ XYZ 机器人）。
> 本文档只做**分析与接入方案设计，未改任何代码**；所有字段口径均对照当前算法
> 实际代码（`run_packing.py` / `src/`，含已实装的 case_group 约束）核实，
> 不含凭空假设。待确认项在 §8 显式列出。
> 2026-07-11 增补：§9 增量/分批订单对接、§10 指数数据本地配置、§11 算法配置
> 的系统对接（对照已实装的 `src/incremental/` 与 `src/adapter/wcs_adapter.py`）。

---

## 1. 接口文档概览（7 个接口，与算法的关系）

WCS 与机器人系统（我方，算法宿主）通过 **HTTP POST + JSON** 相互通讯（状态接口为 GET）。

| # | 接口 | 方向 | 地址 | 与装箱算法的关系 |
|---|---|---|---|---|
| 1 | 库存信息获取 | 机器人→WCS 请求，WCS 返回库存 | `/adaptor/api/wcs/reqstockinfo` | **算法输入来源** |
| 2 | 规划订单输出 | 机器人→WCS | `/adaptor/api/wcs/sendpalletplanresult` | **算法输出去向** |
| 3 | 拼箱物料信息下发 | WCS→机器人 | `/adaptor/api/wcs/sendcasetask` | 执行层（按 `box_unique_id` 取算法方案） |
| 4 | 物料到达 | WCS→机器人 | `/adaptor/api/wcs/boxarrive` | 执行层（逐箱抓取码垛） |
| 5 | 托盘更新 | 机器人→WCS | `/adaptor/api/wcs/reqpallet` | 执行层（托盘管理） |
| 6 | 托盘到达 | WCS→机器人 | `/adaptor/api/wcs/palletarrive` | 执行层 |
| 7 | 获取系统信息 | WCS→机器人（GET，最快 1s/次） | `/api/status` | 设备状态轮询（0 就绪/1 执行中/99 异常） |

**只有接口 1、2 直接进出装箱算法**；接口 3~7 属于机械臂执行/物流控制层，不触碰
算法核心，但依赖算法结果（通过 `box_unique_id` 关联，见 §6）。

另注意版本演进：v1.4/v1.5 **删除了"创建订单/终止订单"接口**（客户改业务逻辑）——
即 WCS 不再向机器人推订单；订单信息随库存条目（`order_id`/`priority`）一起给出。

---

## 2. 对接方式判断

### 2.1 总体形态：机器人侧常驻服务，算法进程内调用

```
┌────────────────────── 机器人侧（我方，算法宿主）──────────────────────┐
│                                                                      │
│  HTTP 客户端（出方向）             HTTP 服务端（入方向，被 WCS 调）     │
│  ├─ POST reqstockinfo  ──输入──┐   ├─ POST sendcasetask   (接口3)     │
│  └─ POST sendpalletplanresult ─┼─  ├─ POST boxarrive      (接口4)     │
│         ▲                      │   ├─ POST palletarrive   (接口6)     │
│         │ 输出(层/顺序视图)     │   └─ GET  /api/status    (接口7)     │
│  ┌──────┴──────────────────────▼──────────────────────────┐          │
│  │ 接口适配层（待实现，算法核心零改动）                       │          │
│  │  输入适配: 聚合库存→逐箱展开 + BMS查指数 + 托盘尺寸映射     │          │
│  │  输出适配: 托盘方案→case(layers/seq/box_unique_id)        │          │
│  └──────┬────────────────────────────────────────▲────────┘          │
│         ▼  boxes(list[dict])                      │ report(dict)     │
│    build_workflow().run_with_boxes(boxes)   ← 现有算法入口，不改       │
│    （完整方案含坐标留在机器人侧，供机械臂垛型执行；对 WCS 只发层/顺序）   │
└──────────────────────────────────────────────────────────────────────┘
```

- **传输方式**：HTTP POST，请求/响应体均为 JSON；统一响应 `{code:0成功/非0失败, msg, data}`。
- **算法调用方式**：进程内 `run_with_boxes(boxes)`（现有公开入口，`docs/可视化系统对接交付文档.md` §7 同款），不落地 Excel、不经 CLI。
- **触发时机**：由机器人侧**主动**发起（接口 1 是机器人→WCS 的请求）。即"拉库存 → 规划 → 推结果"由我方定时/批次/人工触发；WCS 不推订单（创建订单接口已删除）。

### 2.2 数据流时序（规划阶段，与算法直接相关）

1. 机器人侧 POST **接口 1** `reqstockinfo`（带 `msgtime` + 32 位 `msgid`）→ WCS 返回**按品类聚合**的库存数组；
2. 适配层把库存转成算法箱子列表（§3），调用 `run_with_boxes(boxes)` 得到完整装箱报告（含每箱坐标）；
3. 适配层把报告转成**接口 2** 的 case 数组（§4），POST `sendpalletplanresult` 给 WCS；完整报告（坐标版）在机器人侧留存，按 `box_unique_id` 建索引；
4. 执行阶段（不经算法）：WCS 按规划顺序出库 → 接口 3 预告 case → 接口 4 逐箱到达（带 `seq`）→ 机械臂按留存坐标码垛 → 接口 5/6 托盘轮换 → 接口 7 状态轮询。

---

## 3. 输入适配：接口 1（库存）→ 算法箱子列表

### 3.1 接口 1 返回的库存条目（每元素 = 一个**品类**，非逐箱）

```json
{ "length": 700.0, "width": 530.0, "height": 360.0,
  "target_num": 68, "weight": 24.7,
  "box_type": "ZX508", "case_type": "MH423C", "case_group": "0",
  "product_code": 10791358, "order_id": "LAID15455BN01S", "priority": 1 }
```

### 3.2 字段映射表（→ `run_with_boxes` 箱子 dict，现有 schema）

| 接口字段 | 类型 | 算法字段 | 适配规则 |
|---|---|---|---|
| `length`/`width`/`height` | real | `length`/`width`/`height` | 直传（mm） |
| `target_num` | int | —（展开数量） | **一条品类展开成 target_num 个箱子**，内部 `id` 自动生成（建议 `{box_type}-{product_code}-{order_id}-{序号}`，全局唯一；接口世界无逐箱 id，内部 id 不外泄） |
| `weight` | real | `weight` | 直传（单箱重量，算法口径一致） |
| `box_type` | string | `type` | 直传；**并用于查指数**（见缺口①） |
| `case_type` | string | `pallet_type` | 直传（默认 "MH423C"；决定目标指数 `PALLET_INDEX_TARGETS`：MH423C→192） |
| —（接口不提供） | — | `min_pack_multiple` | **缺口①**：接口没有"最小包装量的倍数"。算法达标判定的核心字段，须由算法侧持有 BMS 映射（`box_type → 最小包装量的倍数`，与现 Excel 的 `包装物料主数据(BMS)` sheet 同口径）查得。BMS 数据来源/同步方式**须与企业确认**（§8-1） |
| —（接口不提供） | — | `pallet_dims` | **缺口②**：接口只给 `case_type` 不给托盘尺寸。由适配层按 `case_type` 查本地配置（`config/packing_config.yaml` 的 `pallets` 段，现已配 MH423C=1440×2240×720/192） |
| `case_group` | string，默认 "0" | `case_group` | **直传，已完全就绪**：字段名、类型、默认值 "0"、语义（"组号不为 0 则只能和自己拼 case"）与算法已实装的 case_group 约束逐项一致（`src/utils/case_group.py`，`normalize_case_group("0")→0`；详见 `docs/case_group同组约束接口说明.md`） |
| `product_code` | int | （透传字段） | 存入箱子 dict 原样透传（算法内 dict 复制均保留额外键，已审计），**输出时回填**到接口 2 的 cartons（§4）。同品类展开的箱子共享同一 product_code，回填无歧义 |
| `order_id` | string | `sales_order_no` | 直传（分组键之一：算法按 `(pallet_type, sales_order_no[, case_group])` 分组，组间不混盘） |
| `priority` | int | （透传字段） | 存入箱子 dict 透传；算法核心**当前无优先级概念**，建议仅用于输出 case 排序（§4 box_index）；若需"库存不足时优先满足高优先级订单"的分配语义，属新需求（§8-3） |

### 3.3 输入侧结论

- 算法现有入口**无需改动**即可承接：`run_with_boxes` 的箱子 dict 已支持全部必需字段 + 任意透传字段（case_group 已实装并验证）。
- 适配层需要实现的仅是：**聚合→逐箱展开、BMS 查指数、case_type→托盘尺寸** 三个纯数据转换。
- 两个**数据缺口**（指数、托盘尺寸）接口不提供，必须由我方配置/企业补充——这是对接前置条件，不是代码问题。

---

## 4. 输出适配：算法报告 → 接口 2（规划订单输出）

### 4.1 接口 2 要求的输出（数组，每元素 = 一个拼好的 case/托盘）

```json
{ "box_index": 1, "box_unique_id": "ee1b1286a6f04840a4eabe79f715c4b4",
  "total_height": 720, "order_id": "LAID15465DN09S",
  "case_group": "0", "case_type": "MH423C",
  "layers": [ { "cartons": [
      { "length":700.0, "width":530.0, "height":360.0,
        "layer_id":1, "seq":1, "product_code":10791358 }, ... ] }, ... ] }
```

注（原文）：*发送数据为结构数组…顺序一定是从前往后排列；可能为空 `[]`，代表没有箱子拼 case 成功*。

### 4.2 字段映射表（算法输出 pallet → 接口 case）

算法现输出（`report['pallets'][i]`，schema 见 `docs/可视化系统对接交付文档.md` §5）：
`pallet_id / pallet_type / sales_order_no / case_group(非0组) / mpm_* / fill_rate /
packed_items[{id, type, position{x,y,z}, original_length/width/height, length(+2mm),
min_pack_multiple, ...透传字段}]`。

| 接口字段 | 来源（当前算法输出） | 适配规则 |
|---|---|---|
| `box_index` | 输出顺序 | 按（`priority` 升序 → `order_id` → 盘号）编 1..N，**每批重新从 1 开始**（接口注记） |
| `box_unique_id` | 适配层生成 | `uuid4().hex`（32 字符）；生成后维护 `box_unique_id → 完整托盘方案(含坐标)` 的映射，供接口 3/4 执行阶段取用（接口 3 注记：同 box_unique_id 不能多次下发） |
| `total_height` | `packed_items` | `max(position.z + original_height)`（实际堆叠高度；≤ 托盘 height 上限 720） |
| `order_id` | `sales_order_no` | 直传（算法输出已还原真实订单号，内部后缀不外泄） |
| `case_type` | `pallet_type` | 直传 |
| `case_group` | 盘级 `case_group` 字段 | 非 0 组直传其值；无约束盘输出 `"0"`（接口默认值口径） |
| `layers[].cartons[]` | `packed_items` | 分层与排序规则见 §4.3 |
| `cartons[].length/width/height` | **`original_length/width/height`** | ⚠️ **必须用 `original_*`（真实尺寸，旋转已烘焙）**；不能用 `length/width`（那是 +2mm 间隙的占位尺寸，350→352 会与接口示例 700/530/360 的真实尺寸口径不符） |
| `cartons[].layer_id` | `position.z` | 同一 case 内按 z 值升序去重编号 1..L（同 z 起点 = 同层） |
| `cartons[].seq` | 排序 | **case 内全局连续 1..N，按 `(z, y, x)` 升序**——与算法码垛顺序约定一致（自底向上、从角点扩散，`可视化系统对接交付文档.md` §6 同款），保证 WCS 按 seq 出库、机械臂按序可码垛 |
| `cartons[].product_code` | 箱子透传字段 | 回填输入时存入的 `product_code` |

### 4.3 layers / seq 生成规则（对照算法两条装箱路径）

- **seq（执行顺序，强约束）**：`packed_items` 按 `(position.z, position.y, position.x)`
  升序排序后连续编号。这是算法内部放置顺序的既有约定，两条路径（GCP 柱式 /
  baseline 整层）都满足"先下后上、支撑先行"，直接可执行。
- **layer_id（信息性分层）**：按 z 起点分组。baseline 配方路径是标准整层（z ∈
  {0, h, 2h…}），层语义精确；**GCP 柱式路径**混高箱柱的 z 可能参差（如同盘出现
  z=0/240/360），此时 layer_id 是"按 z 值的近似分层"而非严格物理整层。若机械臂端
  强依赖"整层"语义需与企业确认（§8-4）；若 layer_id 仅作信息展示、执行以 seq 为准，
  则无影响。
- **空结果**：库存为空或无箱可规划时发 `[]`（接口允许）。

### 4.4 未达标盘的处理（重要口径）

接口 2 **没有"达标/未达标"概念**（无 mpm 字段）。算法输出中 `mpm_status=FAILED`
的托盘（指数不足但已尽量装满——实测装到几何上界）默认**也应输出**：算法有
箱子守恒硬门禁（所有箱子必须进托盘、不漏不重），且库存里的箱子最终都要出库
码垛。若业务要求"只发达标 case、剩箱留库"，则属新的业务规则（剩箱如何回库存、
何时重规划），须企业定义（§8-5）。

---

## 5. case_group：接口与算法已完全对齐（无需任何改动）

接口文档两处出现 `case_group`（库存条目 + 规划输出），定义均为：
*string，默认 0，"箱子拼 case 组号，如果组号不为 0 则只能和自己拼 case"*。

与算法已实装的约束逐项对照：

| 维度 | 接口文档 | 当前算法 | 一致性 |
|---|---|---|---|
| 字段名 | `case_group` | `case_group` | ✓ |
| 类型 | string | int/float/str 均可（归一化统一口径，`"0"→0`、`"3"='3'`） | ✓ |
| 默认值 | 0 | 0/缺失＝无约束 | ✓ |
| 语义 | 非 0 只能和自己拼 case | 非 0 只能与同值箱子同托盘（分组隔离＋双层门禁） | ✓ |
| 输出 | case 级 `case_group` | 盘级 `case_group` 字段（非 0 组），0 组适配层补 `"0"` | ✓ |

实装与验证详见 `docs/case_group同组约束接口说明.md`（668 零回归、专项 7 项测试、
覆盖缺口探测 B2 全达最优）。

---

## 6. 执行层接口（3~7）与算法的关系

这些接口**不进算法**，但适配层/机器人服务需要：

| 接口 | 需要算法侧提供什么 |
|---|---|
| 3 拼箱信息下发 | 按 `box_unique_id` 取出该 case 的**完整方案（含坐标）**，机械臂预规划垛型——即 §4.2 生成 box_unique_id 时留存的映射；算法输出的 `position` + `original_*` + 吸盘字段（`suction_*`，算法已按 600×800 吸盘规划每箱抓取姿态）正是垛型执行所需 |
| 4 物料到达 | 按 `box_unique_id + seq` 定位到方案中的具体箱子（seq 即 §4.3 的顺序号），核对尺寸（接口带 length/width/height）后按坐标码垛 |
| 5/6 托盘更新/到达 | 托盘物流管理；接口 5 上报"当前托盘上已码的 cartons"（可从执行进度生成），与规划算法无关 |
| 7 /api/status | 设备状态（0 就绪/1 执行中/99 异常），纯执行层状态机 |

**结论**：算法输出的 JSON 报告已包含执行层所需的全部信息（坐标、真实尺寸、
吸盘姿态、码垛顺序），无需为执行层扩充算法输出——适配层留存映射即可。

---

## 7. 落地改动清单（适配层已实现，2026-07-08；HTTP 服务壳待现场）

全部改动集中在**新增适配层**，算法核心零改动：

1. **✅ 已实现 `src/adapter/wcs_adapter.py`**（纯函数，算法核心零改动）：
   - `stock_to_boxes(stock, bms_map, pallet_dims_map=None)`：§3 输入适配（聚合展开/查指数/托盘尺寸/case_group 归一化/product_code·priority 透传/is_small_box 标记）；未配置托盘尺寸的 case_type 快速失败（当前即 MH110，按决策暂不使用）；
   - `report_to_plan_result(report, include_failed=True) -> WcsPlanResult`：§4 输出适配（box_unique_id=uuid4().hex、box_index 按 priority→订单→原盘序、total_height、layers 按 z 分层、seq 按 (z,y,x)、**original_\* 真实尺寸口径**、product_code 回填、case_group "0"/非 0 直传），并返回 `plan_by_unique_id`（box_unique_id→完整坐标方案）供接口 3/4 执行层取用；
   - 辅助：`load_bms_map(excel)`（BMS 本地过渡，同 excel_loader 口径）、`default_pallet_dims_map()`（读 yaml pallets 段）、`build_stock_request()`（接口 1 请求体）；
   - 全部待确认项以 `TODO(§8-x)` 内联标注（§8-1 BMS 来源、§8-2 MH110、§8-3 priority 方向、§8-4 layer 语义、§8-5 未达标盘、§8-6 空数组判据）。
   - **测试 `tests/test_wcs_adapter.py`（8 项）**：请求体/展开映射/未知 case_type 快速失败/yaml 映射/端到端往返（排序/层/seq/尺寸口径/映射）/case_group 直传/未达标盘开关/空输入。
2. **HTTP 服务壳（待现场框架）**：出方向两个 POST、入方向三个 POST + 一个 GET；统一 `{code, msg, data}` 响应；接口 3 的 box_unique_id 去重由服务壳负责。
3. **配置补充（待企业）**：`packing_config.yaml` 的 `pallets` 段补 MH110 真实尺寸；BMS 映射的正式数据源（本地过渡方案见 §10）。
4. **✅ 联调用例已含**：`test_end_to_end_roundtrip` 用接口文档示例口径做"库存 → 展开 → 规划 → 接口 2 结构"逐字段校验。
5. **增量批次编排（待现场，服务壳内的状态机）**：多批库存到达时维护
   `累计箱清单 / 上一轮报告 / 已推送 case 集合` 三样状态，按 §9 的流程调用
   已实装的 `run_incremental_packing`（算法与增量编排核心零改动）。
6. **配置读写通道（待现场）**：按 §11 暴露白名单字段的查询/修改（文件级或
   HTTP 配置接口二选一），算法零改动。

---

## 8. 待与企业确认清单（对接前置问题；编号与适配层代码内 `TODO(§8-x)` 一一对应）

1. **指数（最小包装量的倍数）数据来源**：接口 1 不提供该字段，而它是"托盘指数达标"目标的核心。BMS 主数据（`box_type → 最小包装量的倍数`）由哪方提供、以何种方式同步（随接口扩展字段 / 定期文件 / 我方维护）？本地过渡方案见 §10。
2. **MH110 托盘尺寸**：接口只给 `case_type` 不给尺寸；MH110 的真实长宽高（及目标指数）待企业提供，补进 `packing_config.yaml` 的 `pallets` 段即自动生效。
3. **priority 语义**：当前仅按"数值小=优先"用于输出 case 排序（box_index）；若需"库存不足时优先满足高优先级订单"的分配语义，属新需求。
4. **layer_id 语义强弱**：机械臂端是否强依赖"物理整层"？GCP 柱式布局的层是按 z 值的近似分层（执行顺序以 seq 为准，无歧义）。
5. **未达标 case 是否输出**：接口无达标概念。默认全部输出（箱子守恒）；若只发达标 case，剩箱的回库/重规划规则需定义。分批场景下的"未达标盘暂扣"见 §9 模式 B 与本清单第 7 项。
6. **空数组判据**：接口 2 注记"可能为空 []，代表没有箱子拼 case 成功"的准确业务判据（库存为空 / 全部规划失败 / 其它）。
7. **分批推送与未达标盘暂扣（§9 模式 B 的前提）**：接口 2 的 box_index"每批重新从 1 开始"注记表明接口支持多批推送；需确认①同一订单的 case 允许分多批推送；②"达标 case 每批即推、未达标盘尾批收尾统一推"的时序是否符合 WCS 出库调度；③"最后一批"的判定信号由谁给（WCS 通知 / 人工触发 / 超时）。
8. **BMS 本地表维护责任**（若采用 §10 本地配置）：谁维护、多久同步一次、出现"库存里有但表里查不到指数"的箱型时按什么策略处理（拦截规划 / 告警放行）。
9. **配置下发通道与权限**（§11）：系统侧改约束用文件下发还是 HTTP 接口；允许修改的字段范围（白名单）；是否要求变更留痕/审计。

---

## 9. 增量/分批订单对接方案（接口分批传入库存）

接口可能**不一次性传来全部订单**：先传一批、算完再传一批。算法侧的增量
计算逻辑**已实现并验证**（`src/incremental/`，README"新增箱/增量装箱测试"
章节，5 个 6000~10000 箱文件实测），对接只需服务壳做批次编排，算法零改动。

### 9.1 已实装的增量语义（编程入口）

```python
from src.incremental import run_incremental_packing

result = run_incremental_packing(
    initial_boxes,     # 此前已参与规划的全部箱子（守恒校验基准）
    new_boxes,         # 本批新到箱子（stock_to_boxes 展开后）
    workflow_factory,  # lambda: build_workflow(constraint_config=cfg)
    initial_report,    # 上一轮完整报告；提供后不会重算初始单
)
merged_report = result.report   # 与普通报告同 schema（多 mode="incremental"）
```

语义（`src/incremental/service.py`）：

- **达标盘冻结**：上一轮 `mpm_status==SUCCESS` 的托盘原样保留（deepcopy，
  布局零改动）——这是分批推送安全性的基石（见 9.3）；
- **只重排"未达标旧箱 + 新箱"**：未达标盘拆散（清除坐标字段），与新批箱子
  合并后走同一套 `run_with_boxes` 完整规划（GCP/救援链/装满压实全部生效）；
- **合并 + 守恒硬门禁**：冻结盘 + 重排结果合并、盘号统一重编；
  `validate_output_quality(全部已收箱, 合并盘)` 不漏箱不重箱，跨批守恒有硬校验；
- **多批链式**：本轮 `result.report` 作为下一轮的 `initial_report`，天然支持
  第 3、4、… 批（未达标箱每轮都获得与最新批合并达标的机会）。

> `run_mode: incremental` + `incremental.source_file`（packing_config.yaml）
> 是**三表 Excel 的测试入口**（`run_incremental_packing.py` 批测同源）；
> 接口对接走上面的编程入口，不依赖 run_mode，但约束配置同一份（§11）。

### 9.2 两种对接模式

| | 模式 A：批间独立 | 模式 B：增量重排（推荐） |
|---|---|---|
| 每批动作 | 单独 `run_with_boxes` 规划本批，**整批立即推送**（含未达标盘） | 增量入口重排"历史未达标箱+新箱"，**只推送达标 case**；未达标盘暂扣机器人侧 |
| 上批未达标盘 | 已推送执行，永远失去与后续箱子合并达标的机会 | 每轮参与重排，达标机会最大化（实测 5000+1000~5000 箱五档，见 README 增量表） |
| 前置条件 | 无（接口现状即可） | §8-7：达标即推 + 尾批收尾的时序需企业确认 |
| 收尾 | 无需 | "最后一批"信号到达后把剩余未达标盘（已尽量装满）统一推送 |

若业务要求"每批到货必须全部即时出库"，只能选 A；否则 B 在达标率上严格占优。

### 9.3 模式 B 服务壳编排（状态机伪代码）

```python
cum_boxes, last_report, pushed_keys = [], None, set()   # 每个规划域一份

def on_new_batch(stock_batch):                 # 接口 1 拉到新一批库存
    global last_report
    new_boxes = stock_to_boxes(stock_batch, bms_map)     # §3 输入适配
    if last_report is None:
        report = build_workflow(cfg).run_with_boxes(new_boxes)   # 首批=普通规划
    else:
        report = run_incremental_packing(
            cum_boxes, new_boxes,
            lambda: build_workflow(constraint_config=cfg),
            initial_report=last_report).report
    cum_boxes.extend(new_boxes)
    last_report = report
    _push(report, only_success=True)           # 达标 case 即批推送

def on_final_flush():                          # 最后一批/人工/超时（§8-7③）
    _push(last_report, only_success=False)     # 未达标盘（已尽量装满）收尾推送

def _push(report, only_success):
    for pallet in report['pallets']:
        key = frozenset(i['id'] for i in pallet['packed_items'])
        if key in pushed_keys:                 # 冻结盘每轮原样出现 → 按箱集去重
            continue
        if only_success and pallet.get('mpm_status') != 'SUCCESS':
            continue
        # report_to_plan_result 同款转换（§4），新盘分配 box_unique_id 并留存映射
        pushed_keys.add(key)
```

**安全性（关键不变式）**：增量入口只溶解**未达标**盘，而模式 B 只推送过
**达标**盘 → 任何已推送 WCS 的 case 永远不会被后续批次重排，不需要"撤回
方案"类接口。推送去重用**箱子 id 集合**识别托盘（合并时盘号会统一重编，
`pallet_id` 是内部号；对外稳定键始终是 `box_unique_id`，§4.2 口径不变）。

> 部署细节：`build_workflow()` 默认每次运行会把报告落盘 `output/`；常驻
> 服务的 `workflow_factory` 建议按 `run_packing._run_incremental` 同款把
> `_report_persister` 置为 `NullReportPersister`（方案留存由服务壳的
> `box_unique_id → 方案` 映射统一负责，避免每轮重复写文件）。

### 9.4 增量对接注意点

- **性能**：每轮重排池 = 未达标旧箱 + 新批箱（不是全量重算），耗时随这两者
  规模走；冻结盘零成本。
- **box_index**：接口注记"每批重新从 1 开始"，与模式 B 的分批推送口径一致
  （每次 `_push` 内部 1..N 重编）。
- **case_group / priority / product_code**：箱子经 `stock_to_boxes` 展开后
  全程透传，增量链路与单批完全同源，无额外适配。
- **暂扣期间**：未达标盘的箱子留在机器人侧规划态、不出库；WCS 侧库存视图
  是否需要感知"已规划未下发"状态，随 §8-7 一并确认。

---

## 10. 指数（BMS）数据本地配置方案

接口 1 不提供"最小包装量的倍数"（§3 缺口①、§8-1），而它是达标判定的核心
输入。在企业给出正式数据源之前，用**本地映射表**过渡——适配层已实现读取
函数，零代码改动即可运行：

### 10.1 本地表格式与加载（现有能力）

- **文件**：本地维护一份 Excel（建议放 `config/bms_local.xlsx`），
  sheet 名固定 **`包装物料主数据(BMS)`**，至少两列：
  **`包装规格代码`**（= 接口的 `box_type`）、**`最小包装量的倍数`**（= 指数）。
  与现有测试数据文件的 BMS sheet **完全同口径**（可直接从企业现行 BMS 导出）。
- **加载**：`bms_map = load_bms_map('config/bms_local.xlsx')`
  （`src/adapter/wcs_adapter.py`，与 excel_loader / 增量 loader 同一解析）；
  每次规划前传入 `stock_to_boxes(stock, bms_map, ...)`。
- **更新流程**：新箱型上线 → 表里加一行 → 下次规划生效。建议服务壳**每次
  规划前重读文件**（表很小，成本可忽略），即改表免重启。

### 10.2 缺失指数的运行语义（务必注意）

`stock_to_boxes` 对查不到指数的箱型**不报错**：按指数 0 展开（与 excel_loader
缺省口径一致）——该箱型照常参与装箱（守恒不受影响），但**无法贡献达标指数**，
所在托盘的达标率会**静默下滑**。因此建议服务壳在展开后加一道运营校验：

```python
missing = sorted({b['type'] for b in boxes if not b['min_pack_multiple']})
if missing:
    告警/拦截（按 §8-8 确认的策略）：f'以下箱型缺指数: {missing}'
```

加载时同样建议校验：指数必须为正数；同一 `包装规格代码` 重复且值冲突时报警。

### 10.3 长期方案（§8-1，三选一，适配层单点切换）

| 来源 | 说明 | 切换成本 |
|---|---|---|
| 接口扩展字段 | 企业在接口 1 库存条目里加指数字段 | 适配层改一行（优先读接口字段，本地表降级为兜底） |
| 定期文件同步 | 企业按周期下发 BMS 文件，覆盖本地表 | 零代码（换文件） |
| 我方维护 | 即 §10.1 本地表转正 | 零代码（明确 §8-8 维护责任即可） |

三种来源最终都汇聚成 `bms_map` 一个入参，算法与适配层其余部分不感知来源。

---

## 11. 算法配置（约束/运行方式）的系统对接

算法的全部可调项都在 **`config/packing_config.yaml`** 一个文件里（单一事实
来源，加载链：`load_constraint_config(path)` → `ConfigLoader` →
`ConstraintConfig`；托盘尺寸/目标指数在 `pallets` 段）。之后的系统若要设置
这些值，按下面口径对接。

### 11.1 生效时机（现有代码语义）

配置在 **`build_workflow(constraint_config=cfg)` 构建工作流时注入**。常驻
服务建议**每次规划前重新 `load_constraint_config()` + `build_workflow()`**
（增量模式的 `workflow_factory` 本就是每轮新建）——于是"改配置 = 下次规划
生效"，无需重启进程。已推送 WCS 的方案不受任何配置变更影响。

### 11.2 系统下发方式（按现场能力二选一，均零算法改动）

1. **文件级下发（最低成本，推荐起步）**：上位机直接写
   `config/packing_config.yaml`（或写到独立路径、服务以 `--config` 指向）。
   注意现有语义：**加载失败会静默回退内置默认值**（不阻断装箱）——服务壳
   必须在加载后回读生效值与下发值比对，不一致即告警，防止"配置写错→默默
   用默认值"难排查。
2. **HTTP 配置接口（服务壳实现）**：`GET /api/config` 返回当前生效配置；
   `PUT /api/config` 收 JSON 片段 → **白名单+范围校验** → 合并写回 yaml
   （持久化，重启不丢）→ 下次规划生效。

### 11.3 字段白名单建议（暴露给系统 vs 算法内部调参）

| yaml 段 | 建议 | 说明 |
|---|---|---|
| `constraints.main_packer` | ✅ 可暴露 | `gcp`（默认）/`beam` 主算法切换 |
| `constraints.*_enabled` 各开关 | ✅ 可暴露 | 吸盘可达/小箱在下/同尺寸重箱在下/倍数分层等可关约束 |
| `constraints.max_box_gap_mm` / `support_ratio_threshold` / `center_of_mass_tolerance` | ✅ 可暴露 | 约束数值（须带范围校验，如支撑率 0~1） |
| `constraints.suction_*` | ✅ 可暴露 | 吸盘尺寸/间隙/旋转，随现场机械臂定 |
| `constraints.allow_box_rotation_90` | ✅ 可暴露 | baseline 路径 90° 旋转开关 |
| `pallets.<型号>.length/width/height/target_index` | ✅ 可暴露 | 新托盘型号 = 加一段即生效（MH110 即走此口径，§8-2） |
| `run_mode` / `incremental.source_file` / `excel_data.*` | ⚠️ 仅文件测试口径 | 接口对接不经它们（§9.1 注），系统侧只读即可 |
| `algorithm.*` / `rescue.*` / `small_box_detection.*` / `tolerances.*` | ❌ 不建议暴露 | 内部调参，影响"耗时×质量"平衡，改动需算法侧评估回归 |

新增一条约束（而非改数值）的扩展路径见 `docs/约束配置与扩展指南.md`
（ConstraintConfig 单一事实来源，放置拦截与门禁同源读取）。

### 11.4 变更留痕（建议）

每次规划把**当次生效配置快照**随留存方案一起记录（`box_unique_id → 方案`
映射旁挂一份 config dump）：达标口径争议/复现问题时可回放"当时用的是哪套
约束"。是否需要正式审计通道随 §8-9 确认。

---

## 12. 一句话总结

**对接方式 = 机器人侧常驻 HTTP 服务内嵌现有算法**：通过接口 1 拉聚合库存 →
适配层展开成箱子列表（补指数/托盘尺寸两个接口缺口，指数本地表方案见 §10）→
现有入口 `run_with_boxes(boxes)` 规划 → 适配层转成接口 2 的 layers/seq 格式
推给 WCS，完整坐标方案留在机器人侧供机械臂执行（接口 3/4 按
`box_unique_id`+`seq` 取用）。**库存分批传入时**走已实装的增量编排
（§9：达标盘冻结、未达标箱与新批合并重排、达标即推/尾批收尾，已推送 case
永不被重排）；**约束与运行方式**经 `packing_config.yaml` 单点对接
（§11：白名单暴露、下次规划生效、快照留痕）。`case_group` 字段接口与算法已
逐项对齐、无需改动；算法核心在整个对接中**零改动**，全部工作集中在一层纯
数据转换的适配器 + 服务壳的批次/配置编排 + 一项企业侧数据补充（BMS 指数）。
