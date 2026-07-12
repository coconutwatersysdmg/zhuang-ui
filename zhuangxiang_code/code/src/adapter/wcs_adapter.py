"""WCS 接口适配层（纯数据转换，算法核心零改动）。

对应《北自柳州五菱项目接口文档》v1.5 与
`docs/WCS接口对接分析与算法接入说明.md`（下称"分析文档"）：

- 输入适配 ``stock_to_boxes()``：接口 1（库存信息获取，
  ``/adaptor/api/wcs/reqstockinfo``）返回的**按品类聚合**库存
  → 算法 ``run_with_boxes(boxes)`` 所需的逐箱列表；
- 输出适配 ``report_to_plan_result()``：算法完整装箱报告
  → 接口 2（规划订单输出，``/adaptor/api/wcs/sendpalletplanresult``）的
  case 数组，并留存 ``box_unique_id → 完整托盘方案（含坐标）`` 映射，
  供执行层接口 3（拼箱信息下发）/ 4（物料到达）按 ``box_unique_id``+``seq``
  取用。

本模块只做字段映射与结构转换，**不发 HTTP**——服务壳（出方向两个 POST、
入方向三个 POST + GET /api/status）按现场框架另行实现（分析文档 §7-2）。
所有待企业确认项以 ``TODO(§8-x)`` 标注，编号对应分析文档 §8 清单。
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.config import ConfigLoader
from src.config.constants import SMALL_BOX_BMS_SHEET
# 复用既有口径：BMS 列校验/解析、is_small_box 体积分位标记（与增量三表同策略）
from src.incremental.loader import _apply_small_box_flags, _build_mpm_index
from src.utils.case_group import normalize_case_group

# 托盘尺寸兜底（与 config/packing_config.yaml 的 MH423C 一致）。正式来源是
# yaml pallets 段；TODO(§8-2): MH110 尺寸待企业提供后补进 yaml 即自动生效，
# 本模块不做 MH110 专门处理（按用户决策暂不使用 MH110 托盘）。
_FALLBACK_PALLET_DIMS: Dict[str, Dict[str, float]] = {
    'MH423C': {'length': 1440.0, 'width': 2240.0, 'height': 720.0},
}

# priority 缺省哨兵：接口声明 priority 非空；若确实缺失按"最低优先级"排最后。
# TODO(§8-3): priority 语义待确认——当前仅按"数值小=优先"用于输出 case 排序，
# 不影响装箱分配；若需"库存不足优先保障高优先级订单达标"属算法新需求。
_PRIORITY_LAST = 10 ** 9


def build_stock_request(msgtime: Optional[str] = None) -> Dict:
    """构造接口 1（库存信息获取）请求体 ``{msgtime, msgid}``。

    TODO: msgtime 格式接口示例为中文日期（"2026年3月19日23:11:19"），
    是否严格要求该格式待现场联调确认；msgid 为 32 字符唯一码（uuid4.hex）。
    """
    if msgtime is None:
        msgtime = datetime.now().strftime('%Y年%m月%d日%H:%M:%S')
    return {'msgtime': msgtime, 'msgid': uuid.uuid4().hex}


def load_bms_map(excel_path) -> Dict[str, float]:
    """从既有 BMS Excel（sheet ``包装物料主数据(BMS)``）读 box_type→指数映射。

    与 excel_loader / incremental loader 完全同口径（``包装规格代码`` →
    ``最小包装量的倍数``）。

    TODO(§8-1): 接口 1 不提供指数字段（达标判定核心）。BMS 数据的正式来源
    /同步方式（随接口扩展字段 / 定期文件 / 我方维护）待企业确认；本函数为
    本地过渡方案。
    """
    df_bms = pd.read_excel(excel_path, sheet_name=SMALL_BOX_BMS_SHEET)
    return _build_mpm_index(df_bms)


def default_pallet_dims_map(config_path: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    """case_type → 托盘尺寸映射（读 ``config/packing_config.yaml`` 的 pallets 段）。

    yaml 缺失/不可读时回退内置 MH423C（1440×2240×720）。当前配置仅含 MH423C；
    TODO(§8-2): MH110 尺寸待企业提供后补进 yaml pallets 段即可（自动带出）。
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / 'config' / 'packing_config.yaml'
    dims: Dict[str, Dict[str, float]] = dict(_FALLBACK_PALLET_DIMS)
    try:
        pallets = ConfigLoader(Path(config_path)).config_data.get('pallets') or {}
    except (OSError, ValueError, KeyError):
        return dims
    for name, cfg in pallets.items():
        try:
            dims[str(name)] = {
                'length': float(cfg['length']),
                'width': float(cfg['width']),
                'height': float(cfg['height']),
            }
        except (KeyError, TypeError, ValueError):
            continue  # 单条配置残缺不阻断其它托盘型
    return dims


def stock_to_boxes(
    stock: List[Dict],
    bms_map: Dict[str, float],
    pallet_dims_map: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[Dict]:
    """接口 1 库存数组（按品类聚合）→ 算法逐箱列表（``run_with_boxes`` 入参）。

    映射规则（分析文档 §3.2）：每条品类展开 ``target_num`` 个箱子；
    ``box_type→type``、``case_type→pallet_type``、``order_id→sales_order_no``；
    指数由 ``bms_map`` 查得（缺失按 0，与 excel_loader 同口径）；托盘尺寸由
    ``pallet_dims_map`` 按 case_type 查得；``case_group`` 归一化直传；
    ``product_code``/``priority`` 透传（输出回填 / case 排序用）。

    Args:
        stock: 接口 1 返回数组，每元素一个品类（含 target_num 数量）。
        bms_map: box_type → 最小包装量的倍数。TODO(§8-1) 来源待企业确认。
        pallet_dims_map: case_type → {length,width,height}；None 用
            ``default_pallet_dims_map()``。

    Returns:
        逐箱字典列表；内部 ``id`` 自动生成（接口世界无逐箱 id，不外泄）。

    Raises:
        ValueError: 出现 ``pallet_dims_map`` 没有的 case_type（缺托盘尺寸
            无法装箱，快速失败）。TODO(§8-2): 当前即 MH110 等未配置型号。
    """
    if pallet_dims_map is None:
        pallet_dims_map = default_pallet_dims_map()
    unknown = sorted({
        str(e.get('case_type')) for e in (stock or [])
        if str(e.get('case_type')) not in pallet_dims_map
    })
    if unknown:
        raise ValueError(
            f'库存含未配置托盘尺寸的 case_type: {unknown}；'
            f'请在 config/packing_config.yaml 的 pallets 段补充（TODO §8-2）。')

    boxes: List[Dict] = []
    for ei, entry in enumerate(stock or []):
        box_type = str(entry.get('box_type'))
        case_type = str(entry.get('case_type'))
        order_id = str(entry.get('order_id') or 'UNKNOWN_ORDER')
        length = float(entry.get('length') or 0)
        width = float(entry.get('width') or 0)
        height = float(entry.get('height') or 0)
        count = int(entry.get('target_num') or 0)
        # 指数：接口不提供，按 BMS 查；缺失按 0（与 excel_loader 缺省一致，
        # 该品类将无法贡献达标指数）。TODO(§8-1)
        mpm = float(bms_map.get(box_type, 0.0))
        dims = pallet_dims_map[case_type]
        for k in range(count):
            boxes.append({
                # 内部逐箱 id：品类内连续编号，跨品类含条目序号保证全局唯一
                'id': f'WCS-{ei:04d}-{box_type}-{k:04d}',
                'type': box_type,
                'length': length, 'width': width, 'height': height,
                'weight': float(entry.get('weight') or 0),
                'min_pack_multiple': mpm,
                'pallet_type': case_type,
                'sales_order_no': order_id,
                'case_group': normalize_case_group(entry.get('case_group', 0)),
                'pallet_dims': dict(dims),
                'volume': length * width * height,
                # —— 透传字段（算法不使用，输出适配回填）——
                'product_code': entry.get('product_code'),
                'priority': entry.get('priority'),
            })
    # is_small_box：与增量三表同策略（体积 25% 分位以下记小箱）
    _apply_small_box_flags(boxes)
    return boxes


@dataclass(frozen=True)
class WcsPlanResult:
    """输出适配结果：接口 2 发送体 + 执行层所需的完整方案映射。"""

    cases: List[Dict] = field(default_factory=list)          # 接口 2 的 JSON 数组
    plan_by_unique_id: Dict[str, Dict] = field(default_factory=dict)
    # ↑ box_unique_id → 算法完整托盘方案（含 position/original_*/suction_*），
    #   供接口 3（预告垛型）/ 4（按 seq 逐箱码垛）取用；接口 3 注记同
    #   box_unique_id 不能多次下发——去重责任在服务壳。


def _case_sort_key(pallet: Dict, orig_idx: int) -> Tuple:
    """case 输出排序：priority 升序（数值小=优先，TODO §8-3 方向待确认）
    → 订单号 → 原盘序（保持算法输出内的相对顺序）。"""
    prios = [
        int(b['priority']) for b in pallet.get('packed_items', [])
        if b.get('priority') is not None
    ]
    prio = min(prios) if prios else _PRIORITY_LAST
    return (prio, str(pallet.get('sales_order_no') or ''), orig_idx)


def _item_xyz(item: Dict) -> Tuple[float, float, float]:
    pos = item.get('position') or {}
    return (
        round(float(pos.get('z', 0) or 0), 3),
        round(float(pos.get('y', 0) or 0), 3),
        round(float(pos.get('x', 0) or 0), 3),
    )


def _true_dim(item: Dict, axis: str) -> float:
    """箱子真实尺寸（旋转已烘焙）。⚠️ 不能用 length/width——那是 +2mm 间隙的
    占位尺寸（350→352），与接口示例的真实尺寸口径不符（分析文档 §4.2）。"""
    return float(
        item.get(f'original_{axis}', item.get(f'raw_{axis}', item.get(axis, 0)))
        or 0)


def _build_layers(items: List[Dict]) -> Tuple[List[Dict], float]:
    """packed_items → (接口 2 layers 结构, total_height)。

    - ``seq``：case 内按 ``(z, y, x)`` 升序连续编号 1..N——与算法既有码垛顺序
      约定一致（自底向上、从角点扩散），WCS 按 seq 出库即可执行；
    - ``layer_id``：按 z 起点分组升序编号 1..L。baseline 整层路径语义精确；
      GCP 柱式混高时为"按 z 的近似分层"。TODO(§8-4): 机械臂端若强依赖物理
      整层语义需企业确认；执行顺序以 seq 为准、无歧义。
    - ``total_height``：实际堆叠高度 max(z + 真实高)。
    """
    ordered = sorted(items, key=_item_xyz)
    z_levels = sorted({_item_xyz(it)[0] for it in ordered})
    layer_of = {z: i + 1 for i, z in enumerate(z_levels)}
    total_height = 0.0
    by_layer: Dict[int, List[Dict]] = {}
    for seq, it in enumerate(ordered, 1):
        z = _item_xyz(it)[0]
        h = _true_dim(it, 'height')
        total_height = max(total_height, z + h)
        lid = layer_of[z]
        by_layer.setdefault(lid, []).append({
            'length': _true_dim(it, 'length'),
            'width': _true_dim(it, 'width'),
            'height': h,
            'layer_id': lid,
            'seq': seq,
            # TODO: 本地 Excel 数据无 product_code（接口库存有）；缺失按 0，
            # 联调前确认 WCS 是否接受 0 或需保证必有值。
            'product_code': int(it.get('product_code') or 0),
        })
    layers = [{'cartons': by_layer[lid]} for lid in sorted(by_layer)]
    return layers, total_height


def report_to_plan_result(
    report: Optional[Dict],
    include_failed: bool = True,
) -> WcsPlanResult:
    """算法装箱报告（``run_with_boxes`` 返回值）→ 接口 2 发送体。

    Args:
        report: 算法完整报告；None 或无托盘 → 空结果（接口允许发 ``[]``，
            "代表没有箱子拼 case 成功"。TODO(§8-6): 空数组的准确业务判据
            待确认）。
        include_failed: 是否包含指数未达标（mpm_status=FAILED）的托盘。
            默认 True——接口无达标概念，且算法有箱子守恒硬门禁（所有箱子
            必须进托盘），库存箱最终都要出库码垛。TODO(§8-5): 若业务要求
            只发达标 case，剩箱回库/重规划规则需企业定义。

    Returns:
        WcsPlanResult(cases=接口 2 数组, plan_by_unique_id=执行层映射)。
        ``box_index`` 按（priority, 订单, 原盘序）从 1 连续编号
        （接口注记"每一批重新更新"、"顺序一定是从前往后排列"）。
    """
    pallets = list((report or {}).get('pallets') or [])
    if not include_failed:
        pallets = [p for p in pallets if p.get('mpm_status') == 'SUCCESS']
    pallets_sorted = sorted(
        enumerate(pallets), key=lambda t: _case_sort_key(t[1], t[0]))

    cases: List[Dict] = []
    plan_by_unique_id: Dict[str, Dict] = {}
    for box_index, (_orig, pallet) in enumerate(pallets_sorted, 1):
        items = pallet.get('packed_items') or []
        if not items:
            continue  # 空托盘不出门（算法门禁本已禁止空盘，双保险）
        layers, total_height = _build_layers(items)
        unique_id = uuid.uuid4().hex  # 32 字符唯一码
        cases.append({
            'box_index': box_index,
            'box_unique_id': unique_id,
            'total_height': total_height,
            'order_id': str(pallet.get('sales_order_no') or ''),
            # 无约束盘输出 "0"（接口默认值口径）；非 0 组直传其值
            'case_group': str(normalize_case_group(pallet.get('case_group'))),
            'case_type': str(pallet.get('pallet_type') or ''),
            'layers': layers,
        })
        plan_by_unique_id[unique_id] = pallet
    return WcsPlanResult(cases=cases, plan_by_unique_id=plan_by_unique_id)
