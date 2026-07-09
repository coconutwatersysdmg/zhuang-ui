"""
直接整层装箱器

为一个 (L,W,H,mpm) 同型批次构造确定性的整层网格装箱。当物品天然可按层
切片填充托盘时，比 beam search 快很多。失败时返回空列表，由上层退回到
通用装箱器。

还提供单箱居中放置的兜底（箱子数 ≤ 4 时使用）。

提取自原 zhuangxiang.build_direct_layer_packing_solution +
build_centered_single_box_solution，去掉对 CustomPacker 的耦合，
改为直接使用 SuctionPlanner 做机械臂可达性检查。
"""

import math
from copy import deepcopy
from typing import Dict, List, Optional

from src.config.constants import MAX_BOX_GAP_MM
from src.geometry.center_of_mass import validate_center_of_mass
from src.geometry.gap_checker import passes_box_gap_constraint
from src.geometry.support import calculate_direct_supported_area, direct_support_ratio
from src.utils.dimensions import raw_dims as get_raw_dims
from src.utils.helpers import (
    apply_suction_pose_fields,
    passes_small_box_not_on_larger_constraint,
    item_volume as get_item_volume,
)

from .layer_pool_builder import build_layer_aware_candidate_pool
from .sanitizer import sanitize_packed_items
from .stacking_policy import (
    build_height_multiple_bonus_by_group,
    build_height_multiple_bonus_by_size,
    passes_same_size_heavier_below_constraint,
    sort_same_size_heavier_first,
    stacking_tiebreak_key,
)
from .suction_planner import SuctionPlanner


def _make_reachability_checker(
    pallet_dims: Dict[str, float], constraint_config=None
) -> SuctionPlanner:
    """构造一个机械臂可达性检查器。

    吸盘几何取自 constraint_config（若提供），否则用与原算法一致的默认值。
    """
    if constraint_config is not None:
        return SuctionPlanner(
            pallet_dims=pallet_dims,
            suction_cup_length=constraint_config.suction_cup_length,
            suction_cup_width=constraint_config.suction_cup_width,
            suction_xy_clearance=constraint_config.suction_xy_clearance,
            suction_z_clearance=constraint_config.suction_z_clearance,
            allow_suction_rotation_90=constraint_config.suction_allow_rotation_90,
        )
    return SuctionPlanner(
        pallet_dims=pallet_dims,
        suction_cup_length=600.0,
        suction_cup_width=800.0,
        suction_xy_clearance=0.0,
        suction_z_clearance=0.0,
        allow_suction_rotation_90=True,
    )


def build_direct_layer_packing_solution(
    items: List[Dict],
    target_mpm: float,
    pallet_dims: Dict[str, float],
    seed: int = 0,
    xy_tolerance: float = 2.0,
    z_tolerance: float = 0.0,
    support_ratio_threshold: float = 0.8,
    candidate_count: int = 12,
    prefer_fill: bool = False,
    constraint_config=None,
) -> List[Dict]:
    """用确定性的层格栅在单托盘内装箱。

    适用于密集、可整层切片的箱子。当候选池可以由完整矩形层切片描述时，
    避开 beam search。

    Args:
        constraint_config: 可选 ConstraintConfig，提供时统一覆盖支撑率、间隙、
            吸盘几何与各可关约束开关；不提供时沿用各参数默认值（行为不变）。

    Returns:
        装箱后的箱子列表（含 position/suction_*/raw_* 字段）。
        无可行方案时返回 []。
    """
    # 约束统一配置：解析开关与数值（不提供则取与历史一致的默认值）。
    if constraint_config is not None:
        support_ratio_threshold = constraint_config.support_ratio_threshold
        max_gap = constraint_config.max_box_gap_mm
        com_tolerance = constraint_config.center_of_mass_tolerance
        small_box_below_enabled = constraint_config.small_box_below_enabled
        same_size_heavier_below_enabled = (
            constraint_config.same_size_heavier_below_enabled
        )
        reachability_enabled = constraint_config.suction_reachability_enabled
    else:
        max_gap = MAX_BOX_GAP_MM
        com_tolerance = 1.0 / 3.0
        small_box_below_enabled = True
        same_size_heavier_below_enabled = True
        reachability_enabled = True

    layer_pools = build_layer_aware_candidate_pool(
        items,
        target_mpm,
        pallet_dims,
        seed=seed,
        xy_tolerance=xy_tolerance,
        z_tolerance=z_tolerance,
        max_overflow=max(32.0, target_mpm * 0.25) if target_mpm else None,
        candidate_count=candidate_count,
        prefer_fill=prefer_fill,
    )
    if not layer_pools:
        layer_pools = []
    elif candidate_count <= 1:
        layer_pools = [layer_pools]

    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    pallet_height = float(pallet_dims.get('height', 0) or 0)
    reachability_checker = _make_reachability_checker(
        pallet_dims, constraint_config
    )
    group_multiple_bonus = build_height_multiple_bonus_by_group(items)
    size_multiple_bonus = build_height_multiple_bonus_by_size(items)

    def _group_pool(pool):
        grouped = {}
        for item in pool:
            key = (
                float(item.get('length', 0) or 0),
                float(item.get('width', 0) or 0),
                float(item.get('height', 0) or 0),
                float(item.get('min_pack_multiple', 0) or 0),
            )
            grouped.setdefault(key, []).append(item)
        return grouped

    def _group_sort_key(entry):
        (length, width, height, mpm), group = entry
        type_label = str(group[0].get('type')) if group else ""
        effective_length = length + xy_tolerance
        effective_width = width + xy_tolerance
        effective_height = height + z_tolerance
        per_x = int(pallet_length // effective_length) if effective_length > 0 else 0
        per_y = int(pallet_width // effective_width) if effective_width > 0 else 0
        per_layer = max(1, per_x * per_y)
        layer_area = (
            min(len(group), per_layer) * effective_length * effective_width
        )
        return (
            -layer_area,
            -group_multiple_bonus.get((length, width, height, mpm), 0.0),
            -effective_length * effective_width,
            -effective_height,
            -mpm,
            type_label,
        )

    def _try_pack_pool(pool):
        grouped_entries = sorted(_group_pool(pool).items(), key=_group_sort_key)
        placed: List[Dict] = []
        current_z = 0.0

        for (length, width, height, mpm), group in grouped_entries:
            effective_length = length + xy_tolerance
            effective_width = width + xy_tolerance
            effective_height = height + z_tolerance
            if (
                effective_length <= 0
                or effective_width <= 0
                or effective_height <= 0
            ):
                return []
            per_x = int(pallet_length // effective_length)
            per_y = int(pallet_width // effective_width)
            per_layer = per_x * per_y
            if per_layer <= 0:
                return []

            ordered_group = sort_same_size_heavier_first(group)
            for idx, item in enumerate(ordered_group):
                layer_idx = idx // per_layer
                slot_idx = idx % per_layer
                point = {
                    'x': float((slot_idx % per_x) * effective_length),
                    'y': float((slot_idx // per_x) * effective_width),
                    'z': float(current_z + layer_idx * effective_height),
                }
                if (
                    point['x'] + effective_length > pallet_length + 1e-9
                    or point['y'] + effective_width > pallet_width + 1e-9
                    or point['z'] + effective_height > pallet_height + 1e-9
                ):
                    return []
                dims = {
                    'length': effective_length,
                    'width': effective_width,
                    'height': effective_height,
                }
                raw_dims = {
                    'length': float(length),
                    'width': float(width),
                    'height': float(height),
                }
                if same_size_heavier_below_enabled and not (
                    passes_same_size_heavier_below_constraint(
                        item,
                        point,
                        dims,
                        placed,
                    )
                ):
                    return []
                if small_box_below_enabled and not (
                    passes_small_box_not_on_larger_constraint(
                        item,
                        point,
                        dims,
                        placed,
                    )
                ):
                    return []
                if not passes_box_gap_constraint(
                    point, dims, raw_dims, placed, max_gap=max_gap
                ):
                    return []
                if (
                    point['z'] > 0
                    and direct_support_ratio(point, dims, placed) + 1e-9
                    < support_ratio_threshold
                ):
                    return []
                if reachability_enabled:
                    suction_pose = reachability_checker.find_reachable_suction_pose(
                        point, dims, placed, raw_dims=raw_dims
                    )
                    if suction_pose is None:
                        return []
                else:
                    suction_pose = None

                item_copy = deepcopy(item)
                item_copy['position'] = point
                item_copy['length'] = effective_length
                item_copy['width'] = effective_width
                item_copy['height'] = effective_height
                item_copy['raw_length'] = raw_dims['length']
                item_copy['raw_width'] = raw_dims['width']
                item_copy['raw_height'] = raw_dims['height']
                if suction_pose is not None:
                    apply_suction_pose_fields(item_copy, suction_pose)
                supported_area = calculate_direct_supported_area(point, dims, placed)
                base_area = effective_length * effective_width
                item_copy['supported_area'] = float(supported_area)
                item_copy['support_ratio'] = (
                    float(supported_area / base_area) if base_area > 0 else 0.0
                )
                placed.append(item_copy)

            current_z += math.ceil(len(group) / per_layer) * effective_height
            if current_z > pallet_height + 1e-9:
                return []

        placed, removed = sanitize_packed_items(
            placed,
            support_ratio_threshold=support_ratio_threshold,
            max_gap=max_gap,
            pallet_dims=pallet_dims,
            center_of_mass_tolerance=com_tolerance,
        )
        if removed:
            return []
        total_mpm = sum(
            float(item.get('min_pack_multiple', 0) or 0) for item in placed
        )
        return placed if total_mpm >= target_mpm else []

    # 第一组兜底候选：同类型最少箱子打满方案
    homogeneous_pools = []
    for key, group in _group_pool(items).items():
        length, width, height, mpm = key
        if mpm <= 0:
            continue
        effective_length = length + xy_tolerance
        effective_width = width + xy_tolerance
        effective_height = height + z_tolerance
        if (
            effective_length <= 0
            or effective_width <= 0
            or effective_height <= 0
        ):
            continue
        per_x = int(pallet_length // effective_length)
        per_y = int(pallet_width // effective_width)
        per_layer = per_x * per_y
        max_layers = int(pallet_height // effective_height)
        max_fit = per_layer * max_layers
        count_needed = int(math.ceil(target_mpm / mpm))
        if (
            per_layer <= 0
            or max_layers <= 0
            or count_needed > min(len(group), max_fit)
        ):
            continue
        pool = sort_same_size_heavier_first(group)[:count_needed]
        overflow = count_needed * mpm - target_mpm
        type_label = str(pool[0].get('type')) if pool else ""
        homogeneous_pools.append(
            (overflow, count_needed, -per_layer, type_label, pool)
        )

    for pool in layer_pools:
        packed = _try_pack_pool(pool)
        if packed:
            return packed

    for _, _, _, _, pool in sorted(homogeneous_pools):
        packed = _try_pack_pool(pool)
        if packed:
            return packed
    return []


def build_centered_single_box_solution(
    items: List[Dict],
    pallet_dims: Dict[str, float],
    xy_tolerance: float = 2.0,
    z_tolerance: float = 0.0,
    support_ratio_threshold: float = 0.8,
    constraint_config=None,
) -> List[Dict]:
    """单箱居中放置兜底（用于剩余 <= 4 箱时）。

    单箱（z=0、无堆叠、无邻居）只受边界/重心/吸盘约束影响；constraint_config
    提供时透传吸盘几何、可达性开关与重心偏差，不提供时行为不变。
    """
    reachability_enabled = (
        constraint_config.suction_reachability_enabled
        if constraint_config is not None else True
    )
    com_tolerance = (
        constraint_config.center_of_mass_tolerance
        if constraint_config is not None else 1.0 / 3.0
    )
    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    pallet_height = float(pallet_dims.get('height', 0) or 0)
    reachability_checker = _make_reachability_checker(
        pallet_dims, constraint_config
    )
    size_multiple_bonus = build_height_multiple_bonus_by_size(items)

    ordered_items = sorted(
        items,
        key=lambda item: (
            -float(item.get('min_pack_multiple', 0) or 0),
            stacking_tiebreak_key(item, size_multiple_bonus),
            -get_item_volume(item),
            str(item.get('id')),
        ),
    )
    for item in ordered_items:
        raw_dims = {
            'length': float(item.get('length', 0) or 0),
            'width': float(item.get('width', 0) or 0),
            'height': float(item.get('height', 0) or 0),
        }
        dims = {
            'length': raw_dims['length'] + xy_tolerance,
            'width': raw_dims['width'] + xy_tolerance,
            'height': raw_dims['height'] + z_tolerance,
        }
        if (
            dims['length'] <= 0
            or dims['width'] <= 0
            or dims['height'] <= 0
            or dims['length'] > pallet_length + 1e-9
            or dims['width'] > pallet_width + 1e-9
            or dims['height'] > pallet_height + 1e-9
        ):
            continue

        point = {
            'x': (pallet_length - dims['length']) / 2.0,
            'y': (pallet_width - dims['width']) / 2.0,
            'z': 0.0,
        }
        if reachability_enabled:
            suction_pose = reachability_checker.find_reachable_suction_pose(
                point, dims, [], raw_dims=raw_dims
            )
            if suction_pose is None:
                continue
        else:
            suction_pose = None

        item_copy = deepcopy(item)
        item_copy['position'] = point
        item_copy['length'] = dims['length']
        item_copy['width'] = dims['width']
        item_copy['height'] = dims['height']
        item_copy['raw_length'] = raw_dims['length']
        item_copy['raw_width'] = raw_dims['width']
        item_copy['raw_height'] = raw_dims['height']
        if suction_pose is not None:
            apply_suction_pose_fields(item_copy, suction_pose)
        base_area = dims['length'] * dims['width']
        item_copy['supported_area'] = float(base_area)
        item_copy['support_ratio'] = 1.0 if base_area > 0 else 0.0
        if validate_center_of_mass(
            {'packed_items': [item_copy]}, pallet_dims, tolerance=com_tolerance
        ).get('is_stable', False):
            return [item_copy]

    return []
