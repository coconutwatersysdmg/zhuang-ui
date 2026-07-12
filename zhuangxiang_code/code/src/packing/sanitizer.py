"""
装箱后清理器

对一组已放置的箱子做合规性扫描：移除违反间隙、支撑、重心约束的箱子，
返回 (kept, removed) 二元组。removed 中的箱子会被还原为可重装状态
（无 position / suction），可以喂回装箱器。

提取自原 zhuangxiang._sanitize_packed_items，对外暴露顶层函数，避免
BeamSearchPacker 与 direct-layer packer 各自维护一份重复实现。
"""

from copy import deepcopy
from typing import Dict, List, Optional, Tuple

from src.config.constants import MAX_BOX_GAP_MM
from src.geometry.center_of_mass import validate_center_of_mass
from src.geometry.gap_checker import passes_box_gap_constraint
from src.utils.dimensions import raw_dims as get_raw_dims
from src.utils.helpers import (
    has_box_above,
    refresh_support_metrics,
    repack_ready_item,
)


def sanitize_packed_items(
    items: List[Dict],
    support_ratio_threshold: float = 0.8,
    max_gap: float = MAX_BOX_GAP_MM,
    pallet_dims: Optional[Dict] = None,
    center_of_mass_tolerance: float = 1.0 / 3.0,
) -> Tuple[List[Dict], List[Dict]]:
    """清理不合法的放置。

    依次执行 3 类检查并循环扣减：
    1. 间隙约束（最近邻 Z 区间重叠的箱子，正间隙必须 < max_gap）
    2. 支撑约束（z>0 的箱子的 support_ratio >= 阈值）
    3. 重心约束（仅当 pallet_dims 提供；偏移比例阈值 center_of_mass_tolerance）

    每次只移除"最该被去掉"的箱子（顶部、低 mpm、Z 高的优先），
    避免一次性扔太多。

    center_of_mass_tolerance 与门禁 validate_pallet_constraints 用同一阈值，
    保证放置层清理与最终门禁同源。

    Returns:
        (kept_items, removed_items)。removed 已用 repack_ready_item 还原。
    """
    kept_items = [deepcopy(item) for item in items]
    removed_items: List[Dict] = []

    while kept_items:
        # 1. 间隙约束
        gap_violators = [
            item for item in kept_items
            if not passes_box_gap_constraint(
                item['position'],
                {
                    'length': item['length'],
                    'width': item['width'],
                    'height': item['height'],
                },
                get_raw_dims(item),
                [other for other in kept_items if other.get('id') != item.get('id')],
                max_gap=max_gap,
                pallet_dims=pallet_dims,
            )
        ]
        if gap_violators:
            victim = min(
                gap_violators,
                key=lambda item: (
                    1 if has_box_above(item, kept_items) else 0,
                    float(item.get('min_pack_multiple', 0) or 0),
                    -float(item['position']['z']),
                    -float(item['position']['y']),
                    -float(item['position']['x']),
                    str(item.get('id')),
                ),
            )
            kept_items = [
                item for item in kept_items
                if item.get('id') != victim.get('id')
            ]
            removed_items.append(repack_ready_item(victim))
            continue

        # 2. 支撑约束
        refresh_support_metrics(kept_items)
        unstable_items = [
            item for item in kept_items
            if item['position']['z'] > 1e-9
            and item.get('support_ratio', 0.0) + 1e-9 < support_ratio_threshold
        ]
        if not unstable_items:
            # 3. 重心约束
            if pallet_dims:
                com_result = validate_center_of_mass(
                    {'packed_items': kept_items}, pallet_dims,
                    tolerance=center_of_mass_tolerance,
                )
                if not com_result.get('is_stable', False):
                    victim = _select_com_victim(
                        kept_items, pallet_dims, center_of_mass_tolerance
                    )
                    if victim is None:
                        break
                    kept_items = [
                        item for item in kept_items
                        if item.get('id') != victim.get('id')
                    ]
                    removed_items.append(repack_ready_item(victim))
                    continue
            break

        victim = min(
            unstable_items,
            key=lambda item: (
                0 if has_box_above(item, kept_items) else 1,
                -float(item['position']['z']),
                float(item.get('min_pack_multiple', 0) or 0),
                -float(item['position']['y']),
                -float(item['position']['x']),
                str(item.get('id')),
            ),
        )
        kept_items = [
            item for item in kept_items
            if item.get('id') != victim.get('id')
        ]
        removed_items.append(repack_ready_item(victim))

    if kept_items:
        refresh_support_metrics(kept_items)
    return kept_items, removed_items


def _select_com_victim(
    kept_items: List[Dict],
    pallet_dims: Dict,
    center_of_mass_tolerance: float = 1.0 / 3.0,
) -> Optional[Dict]:
    """挑出对重心偏移贡献最大的可拿走箱子。"""
    pallet_center_x = float(pallet_dims['length']) / 2.0
    pallet_center_y = float(pallet_dims['width']) / 2.0
    total_weight = sum(
        float(item.get('weight', 0.0) or 0.0) for item in kept_items
    )
    if total_weight <= 0:
        return None

    moment_x = 0.0
    moment_y = 0.0
    for item in kept_items:
        pos = item['position']
        weight = float(item.get('weight', 0.0) or 0.0)
        cx = pos['x'] + float(item.get('length', 0.0) or 0.0) / 2.0
        cy = pos['y'] + float(item.get('width', 0.0) or 0.0) / 2.0
        moment_x += weight * cx
        moment_y += weight * cy
    com_x = moment_x / total_weight
    com_y = moment_y / total_weight
    offset_x = com_x - pallet_center_x
    offset_y = com_y - pallet_center_y
    sign_x = 1.0 if offset_x >= 0 else -1.0
    sign_y = 1.0 if offset_y >= 0 else -1.0
    tol_x = float(pallet_dims['length']) * center_of_mass_tolerance
    tol_y = float(pallet_dims['width']) * center_of_mass_tolerance
    use_x = abs(offset_x) > tol_x + 1e-9
    use_y = abs(offset_y) > tol_y + 1e-9

    return max(
        kept_items,
        key=lambda item: (
            1 if not has_box_above(item, kept_items) else 0,
            (
                max(
                    0.0,
                    sign_x * (
                        item['position']['x']
                        + float(item.get('length', 0.0) or 0.0) / 2.0
                        - pallet_center_x
                    ),
                ) * float(item.get('weight', 0.0) or 0.0)
                if use_x else 0.0
            ) + (
                max(
                    0.0,
                    sign_y * (
                        item['position']['y']
                        + float(item.get('width', 0.0) or 0.0) / 2.0
                        - pallet_center_y
                    ),
                ) * float(item.get('weight', 0.0) or 0.0)
                if use_y else 0.0
            ),
            float(item['position']['z']),
            -float(item.get('min_pack_multiple', 0) or 0),
            str(item.get('id')),
        ),
    )
