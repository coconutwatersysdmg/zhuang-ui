"""
箱间间隙检查函数

提供箱子间隙约束验证和间隙标记功能。
"""

from typing import Dict, List
from .overlap import axis_overlap_len


def passes_box_gap_constraint(
    point: Dict[str, float],
    dims: Dict[str, float],
    raw_dims: Dict[str, float],
    placed_boxes: List[Dict],
    max_gap: float = 6.0
) -> bool:
    """
    检查箱子在指定位置是否满足箱间间隙约束

    验证候选箱子与同层相邻箱子之间的间隙是否小于最大允许间隙。

    Args:
        point: 候选位置 {'x': float, 'y': float, 'z': float}
        dims: 候选箱子的有效尺寸（包含容差）
        raw_dims: 候选箱子的原始尺寸（不含容差）
        placed_boxes: 已放置的箱子列表
        max_gap: 最大允许间隙（毫米），默认6.0

    Returns:
        如果满足间隙约束返回True，否则返回False

    Notes:
        - 邻居判定使用 Z 区间重叠（而非 Z 坐标严格相等），覆盖跨层并排放置
          的箱子，避免漏判
        - 检查四个方向（x_min, x_max, y_min, y_max）的最近间隙
        - 如果某个方向没有 Z 区间重叠且垂直方向投影重叠的邻箱，该方向不受约束
        - 托盘墙不参与本约束

    Examples:
        >>> point = {'x': 100, 'y': 0, 'z': 0}
        >>> dims = {'length': 100, 'width': 100, 'height': 100}
        >>> raw_dims = {'length': 98, 'width': 98, 'height': 100}
        >>> placed = [{
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100,
        ...     'raw_length': 98,
        ...     'raw_width': 98,
        ...     'raw_height': 100
        ... }]
        >>> passes_box_gap_constraint(point, dims, raw_dims, placed, max_gap=6.0)
        True
    """
    if not isinstance(raw_dims, dict):
        raw_dims = dims

    eps = 1e-9
    x_min = float(point['x'])
    x_max = x_min + float(raw_dims['length'])
    y_min = float(point['y'])
    y_max = y_min + float(raw_dims['width'])
    z_min = float(point['z'])
    z_max = z_min + float(raw_dims['height'])

    # 记录四个方向的最近间隙
    nearest_gaps = {'x_min': None, 'x_max': None, 'y_min': None, 'y_max': None}

    for placed_box in placed_boxes:
        placed_pos = placed_box.get('position')
        if not placed_pos:
            continue

        # 获取已放置箱子的原始尺寸
        placed_raw_dims = {
            'length': float(placed_box.get('raw_length', placed_box.get('length', 0)) or 0),
            'width': float(placed_box.get('raw_width', placed_box.get('width', 0)) or 0),
            'height': float(placed_box.get('raw_height', placed_box.get('height', 0)) or 0),
        }

        px_min = float(placed_pos['x'])
        px_max = px_min + placed_raw_dims['length']
        py_min = float(placed_pos['y'])
        py_max = py_min + placed_raw_dims['width']
        pz_min = float(placed_pos['z'])
        pz_max = pz_min + placed_raw_dims['height']

        # Z 区间重叠才认为是有效邻居（非严格同层）
        z_overlap = axis_overlap_len(z_min, z_max, pz_min, pz_max) > eps
        if not z_overlap:
            continue

        # 检查Y方向重叠（用于X方向间隙计算）
        if axis_overlap_len(y_min, y_max, py_min, py_max) > eps:
            # 左侧间隙（候选箱子左边缘到已放置箱子右边缘）
            left_gap = x_min - px_max
            # 右侧间隙（已放置箱子左边缘到候选箱子右边缘）
            right_gap = px_min - x_max

            if left_gap >= -eps:
                nearest_gaps['x_min'] = left_gap if nearest_gaps['x_min'] is None else min(nearest_gaps['x_min'], left_gap)
            if right_gap >= -eps:
                nearest_gaps['x_max'] = right_gap if nearest_gaps['x_max'] is None else min(nearest_gaps['x_max'], right_gap)

        # 检查X方向重叠（用于Y方向间隙计算）
        if axis_overlap_len(x_min, x_max, px_min, px_max) > eps:
            # 前侧间隙（候选箱子前边缘到已放置箱子后边缘）
            front_gap = y_min - py_max
            # 后侧间隙（已放置箱子前边缘到候选箱子后边缘）
            back_gap = py_min - y_max

            if front_gap >= -eps:
                nearest_gaps['y_min'] = front_gap if nearest_gaps['y_min'] is None else min(nearest_gaps['y_min'], front_gap)
            if back_gap >= -eps:
                nearest_gaps['y_max'] = back_gap if nearest_gaps['y_max'] is None else min(nearest_gaps['y_max'], back_gap)

    # 每个方向：要么没有有效邻居（None），要么最近正间隙 < max_gap
    return all(gap is None or gap < max_gap - eps for gap in nearest_gaps.values())


def side_gap_flags(
    point: Dict[str, float],
    raw_dims: Dict[str, float],
    placed_boxes: List[Dict],
    max_gap: float = 6.0
) -> Dict[str, bool]:
    """
    标记候选位置四个方向是否有小间隙

    Args:
        point: 候选位置 {'x': float, 'y': float, 'z': float}
        raw_dims: 候选箱子的原始尺寸
        placed_boxes: 已放置的箱子列表
        max_gap: 小间隙阈值（毫米），默认6.0

    Returns:
        四个方向的间隙标记 {'x_min': bool, 'x_max': bool, 'y_min': bool, 'y_max': bool}
        True表示该方向有小于max_gap的间隙

    Examples:
        >>> point = {'x': 100, 'y': 0, 'z': 0}
        >>> raw_dims = {'length': 98, 'width': 98, 'height': 100}
        >>> placed = [{
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }]
        >>> side_gap_flags(point, raw_dims, placed, max_gap=6.0)
        {'x_min': True, 'x_max': False, 'y_min': False, 'y_max': False}
    """
    eps = 1e-9
    x_min = float(point['x'])
    x_max = x_min + float(raw_dims['length'])
    y_min = float(point['y'])
    y_max = y_min + float(raw_dims['width'])
    z_min = float(point['z'])
    z_max = z_min + float(raw_dims['height'])

    nearest_gaps = {'x_min': None, 'x_max': None, 'y_min': None, 'y_max': None}

    for placed_box in placed_boxes:
        placed_pos = placed_box.get('position')
        if not placed_pos:
            continue

        placed_raw_dims = {
            'length': float(placed_box.get('raw_length', placed_box.get('length', 0)) or 0),
            'width': float(placed_box.get('raw_width', placed_box.get('width', 0)) or 0),
            'height': float(placed_box.get('raw_height', placed_box.get('height', 0)) or 0),
        }

        px_min = float(placed_pos['x'])
        px_max = px_min + placed_raw_dims['length']
        py_min = float(placed_pos['y'])
        py_max = py_min + placed_raw_dims['width']
        pz_min = float(placed_pos['z'])
        pz_max = pz_min + placed_raw_dims['height']

        # Z 区间重叠（非严格同层）
        if axis_overlap_len(z_min, z_max, pz_min, pz_max) <= eps:
            continue

        if axis_overlap_len(y_min, y_max, py_min, py_max) > eps:
            left_gap = x_min - px_max
            right_gap = px_min - x_max
            if left_gap >= -eps:
                nearest_gaps['x_min'] = left_gap if nearest_gaps['x_min'] is None else min(nearest_gaps['x_min'], left_gap)
            if right_gap >= -eps:
                nearest_gaps['x_max'] = right_gap if nearest_gaps['x_max'] is None else min(nearest_gaps['x_max'], right_gap)

        if axis_overlap_len(x_min, x_max, px_min, px_max) > eps:
            front_gap = y_min - py_max
            back_gap = py_min - y_max
            if front_gap >= -eps:
                nearest_gaps['y_min'] = front_gap if nearest_gaps['y_min'] is None else min(nearest_gaps['y_min'], front_gap)
            if back_gap >= -eps:
                nearest_gaps['y_max'] = back_gap if nearest_gaps['y_max'] is None else min(nearest_gaps['y_max'], back_gap)

    return {
        key: (gap is not None and gap < max_gap - eps)
        for key, gap in nearest_gaps.items()
    }


def boundary_side_flags(
    point: Dict[str, float],
    raw_dims: Dict[str, float],
    pallet_dims: Dict[str, float],
    max_gap: float = 6.0
) -> Dict[str, bool]:
    """
    标记候选位置四个方向是否靠近托盘边界

    Args:
        point: 候选位置 {'x': float, 'y': float, 'z': float}
        raw_dims: 候选箱子的原始尺寸
        pallet_dims: 托盘尺寸 {'length': float, 'width': float}
        max_gap: 边界间隙阈值（毫米），默认6.0

    Returns:
        四个方向的边界标记 {'x_min': bool, 'x_max': bool, 'y_min': bool, 'y_max': bool}
        True表示该方向距离托盘边界小于max_gap

    Examples:
        >>> point = {'x': 2, 'y': 0, 'z': 0}
        >>> raw_dims = {'length': 100, 'width': 100, 'height': 100}
        >>> pallet_dims = {'length': 1200, 'width': 1000}
        >>> boundary_side_flags(point, raw_dims, pallet_dims, max_gap=6.0)
        {'x_min': True, 'x_max': False, 'y_min': True, 'y_max': False}
    """
    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    x_min = float(point['x'])
    x_max = x_min + float(raw_dims['length'])
    y_min = float(point['y'])
    y_max = y_min + float(raw_dims['width'])
    eps = 1e-9

    return {
        'x_min': x_min < max_gap - eps,
        'x_max': pallet_length - x_max < max_gap - eps,
        'y_min': y_min < max_gap - eps,
        'y_max': pallet_width - y_max < max_gap - eps,
    }
