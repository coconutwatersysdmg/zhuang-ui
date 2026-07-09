"""列式(分层)装箱器：底面分组 → 凑高成柱 → 列网格摆放。

作为"配方优先"的强化前置路径：对"整单恰好一托盘"的分组，按
"同底面垂直堆叠成柱（支撑率天然 1.0）+ 列网格平面摆放"直接构造单托盘
满载方案。装出达标方案（守恒 + 整盘门禁通过）则采用，否则返回 None
回落现有配方/基线流程——纯增量、零回归。

适用前提（不满足则优雅回落）：
- 分组总指数落在单托盘区间 [target, target×1.5)；
- 箱型底面与列宽网格匹配（本项目为含 350/265/700 公共边的规整箱型）。

几何口径与主装箱一致：箱实际占用 = 原始尺寸 + size_tolerance，按 z
从低到高放置，逐箱复用 packer 的 SuctionPlanner 求吸盘姿态。
"""

from collections import defaultdict
from copy import deepcopy
from typing import Dict, List, Optional

from ..geometry.constraint_validator import validate_pallet_constraints
from ..utils.dimensions import raw_dims
from .beam_search_packer import BeamSearchPacker

COLUMN_WIDTH = 350.0  # 列网格主列宽（mm），匹配本项目规整底面的公共边


def _ffd_columns(boxes_fp: List[Dict], cap: float) -> List[List[Dict]]:
    """同底面箱按高度降序 First-Fit-Decreasing 凑成柱（柱内高度和 ≤ cap）。"""
    cols: List[Dict] = []
    for box in sorted(boxes_fp, key=lambda b: -float(b.get('height', 0) or 0)):
        h = float(box.get('height', 0) or 0)
        for col in cols:
            if col['rem'] >= h - 1e-9:
                col['rem'] -= h
                col['boxes'].append(box)
                break
        else:
            cols.append({'rem': cap - h, 'boxes': [box]})
    return [c['boxes'] for c in cols]


def _build_columns(boxes: List[Dict], pallet_dims: Dict[str, float]) -> List[Dict]:
    """按底面分组凑柱。返回 [{xlen, ylen, boxes}]，xlen 让 350/700 公共边沿 x。"""
    cap = float(pallet_dims.get('height', 0) or 0)
    by_fp: Dict[tuple, List[Dict]] = defaultdict(list)
    for box in boxes:
        key = tuple(sorted((
            int(round(float(box.get('length', 0) or 0))),
            int(round(float(box.get('width', 0) or 0))),
        )))
        by_fp[key].append(box)
    cols: List[Dict] = []
    for key, group in by_fp.items():
        if 700 in key:
            xlen, ylen = 700.0, float(key[0] if key[1] == 700 else key[1])
        elif 350 in key:
            xlen, ylen = 350.0, float(key[0] if key[1] == 350 else key[1])
        else:
            xlen, ylen = float(key[0]), float(key[1])
        for boxes_in_col in _ffd_columns(group, cap):
            cols.append({'xlen': xlen, 'ylen': ylen, 'boxes': boxes_in_col})
    return cols


def _grid_pack(
    cols: List[Dict], pallet_dims: Dict[str, float], tol: float
) -> Optional[List[tuple]]:
    """列网格摆柱：700 柱跨连续列、单列柱均衡放到最矮列。返回 [(col, x, y)] 或 None。"""
    pall = float(pallet_dims.get('length', 0) or 0)
    palw = float(pallet_dims.get('width', 0) or 0)
    colstep = COLUMN_WIDTH + tol
    ncols = int(pall // colstep)
    if ncols < 1:
        return None
    xs = [c * colstep for c in range(ncols)]
    col_y = [0.0] * ncols
    placements: List[tuple] = []

    wide = [c for c in cols if c['xlen'] > colstep]
    single = [c for c in cols if c['xlen'] <= colstep]
    for c in wide:
        span = int(-(-(c['xlen'] + tol) // colstep))  # ceil 列数
        best = None
        for s in range(ncols - span + 1):
            top = max(col_y[s:s + span])
            if best is None or top < best[1] - 1e-9:
                best = (s, top)
        if best is None:
            return None
        s, top = best
        if top + c['ylen'] + tol > palw + 1e-6:
            return None
        placements.append((c, xs[s], top))
        for k in range(s, s + span):
            col_y[k] = top + c['ylen'] + tol
    for c in sorted(single, key=lambda c: -c['ylen']):
        need = c['ylen'] + tol
        best, best_y = -1, 1e18
        for col in range(ncols):
            if col_y[col] + need <= palw + 1e-6 and col_y[col] < best_y - 1e-9:
                best, best_y = col, col_y[col]
        if best < 0:
            return None
        placements.append((c, xs[best], col_y[best]))
        col_y[best] += need
    return placements


def _assemble(
    placements: List[tuple], packer, pallet_dims: Dict[str, float],
    gap: Optional[float] = None,
) -> List[Dict]:
    """柱位置 → 逐柱 z 堆叠 → packed_items（含吸盘字段，几何口径与主装箱一致）。

    gap：柱间水平容差，柱占地 = 底面尺寸 + gap。None 用 packer.size_tolerance
    （265 网格落地，柱间留缝）；CP-SAT 紧贴落地传 0（柱坐标已精确无重叠）。
    """
    tol = packer.size_tolerance if gap is None else float(gap)
    ztol = packer.z_tolerance
    layout = []
    for c, x, y in placements:
        z = 0.0
        for box in c['boxes']:  # 已按高度降序，大箱在下
            layout.append({
                'box': box, 'x': x, 'y': y, 'z': z,
                'xlen': c['xlen'], 'ylen': c['ylen'],
            })
            z += float(box.get('height', 0) or 0)

    placed: List[Dict] = []
    for spec in sorted(layout, key=lambda s: (s['z'], s['y'], s['x'])):
        box, xlen, ylen = spec['box'], spec['xlen'], spec['ylen']
        h = float(box.get('height', 0) or 0)
        item = deepcopy(box)
        item['raw_length'] = float(xlen)
        item['raw_width'] = float(ylen)
        item['raw_height'] = h
        point = {'x': float(spec['x']), 'y': float(spec['y']), 'z': float(spec['z'])}
        dims = {'length': xlen + tol, 'width': ylen + tol, 'height': h + ztol}
        pose = None
        if packer.robot_reachability_enabled:
            pose = packer.suction_planner.find_reachable_suction_pose(
                point, dims, placed, raw_dims=raw_dims(item)
            )
        item['position'] = point
        item['length'] = dims['length']
        item['width'] = dims['width']
        item['height'] = dims['height']
        if pose:
            item['suction_box_corner'] = pose['box_corner']
            item['suction_cup_corner'] = pose['cup_corner']
            item['suction_orientation'] = pose['orientation']
            item['suction_cup_x_size'] = pose['cup_x_size']
            item['suction_cup_y_size'] = pose['cup_y_size']
            item['suction_rect_x_min'] = pose['cup_rect']['x_min']
            item['suction_rect_x_max'] = pose['cup_rect']['x_max']
            item['suction_rect_y_min'] = pose['cup_rect']['y_min']
            item['suction_rect_y_max'] = pose['cup_rect']['y_max']
        # 标记：raw_* 已按放置朝向写入（可能相对原始箱旋转 90°），输出阶段须保留
        item['layered_oriented'] = True
        placed.append(item)
    return placed


def try_layered_order(
    packer,
    boxes: List[Dict],
    target_mpm: Optional[float],
    pallet_dims: Dict[str, float],
) -> Optional[List[Dict]]:
    """尝试把"整单恰好一托盘"的分组用列式装箱装成单个达标托盘。

    成功（守恒 + 指数达标 + 整盘门禁通过）返回 [pallet_solution]；
    任何不满足条件的情况返回 None，调用方据此回落配方/基线流程。

    Args:
        packer: 主装箱器实例，复用其 suction_planner / 容差 / 约束配置。
        boxes: 该分组的全部箱子。
        target_mpm: 该托盘类型的目标指数。
        pallet_dims: 托盘尺寸。

    Returns:
        单元素托盘方案列表，或 None。
    """
    if not boxes or target_mpm is None or target_mpm <= 0:
        return None
    total_mpm = sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes)
    # 仅处理单托盘场景：整单指数落在 [target, target×1.5)，避免误吞多盘分组
    if not (target_mpm - 1e-9 <= total_mpm < target_mpm * 1.5):
        return None

    cfg = getattr(packer, '_cfg', None)
    # 复用主装箱的几何口径：用同一份约束配置构造 helper，其容差/吸盘几何/
    # 支撑率与主装箱一致，保证 assemble 出的方案与主流程同源。
    helper = BeamSearchPacker(pallet_dims=pallet_dims, constraint_config=cfg)
    cols = _build_columns(boxes, pallet_dims)
    placements = _grid_pack(cols, pallet_dims, helper.size_tolerance)
    if placements is None:
        return None
    placed = _assemble(placements, helper, pallet_dims)

    in_ids = {b.get('id') for b in boxes}
    out_ids = [b.get('id') for b in placed]
    if set(out_ids) != in_ids or len(out_ids) != len(boxes):
        return None
    idx = sum(float(b.get('min_pack_multiple', 0) or 0) for b in placed)
    if idx + 1e-9 < target_mpm:
        return None
    gate = validate_pallet_constraints(
        {'packed_items': placed}, pallet_dims,
        constraint_config=cfg,
    )
    if not gate.get('is_valid'):
        return None

    return [{
        'pallet_id': None,
        'packed_items': placed,
        'mpm_total': idx,
        'mpm_target': target_mpm,
        'mpm_gap': target_mpm - idx,
        'mpm_status': 'SUCCESS',
        'stability_checks': {'status': 'SUCCESS'},
    }]
