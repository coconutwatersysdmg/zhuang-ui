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
    max_gap: float = 6.0,
    pallet_dims: Dict[str, float] = None,
) -> bool:
    """
    检查箱子在指定位置是否满足箱间间隙约束（锚定语义）

    约束本意是"箱子逐个、贴紧摆放"，防止能推齐而不推齐的偷懒浮空；
    不禁止贴紧一侧后对面残余的不可避免间隙（行尾残缝、混底面槽位残缝）。

    锚定判定：X、Y 每个轴上，满足以下任一条件即视为"贴紧摆放"：
        1. 该轴任一侧与最近邻箱的正间隙 < max_gap（贴紧邻箱）；
        2. 该轴任一侧距托盘边界 < max_gap（推到托盘边，需提供 pallet_dims；
           x_min/y_min 侧靠原点，无需 pallet_dims 也可判定）；
        3. 该轴两侧均无有效邻箱（无可贴紧对象，不受约束）。
    两轴均锚定则通过；某轴存在邻箱、既未贴紧邻箱也未靠边则拒绝。

    该语义是旧"四方向最近正间隙必须 < max_gap"规则的纯放宽：
    旧规则接受的摆放（各方向间隙全部 < max_gap 或无邻箱）必然两轴锚定。

    Args:
        point: 候选位置 {'x': float, 'y': float, 'z': float}
        dims: 候选箱子的有效尺寸（包含容差）
        raw_dims: 候选箱子的原始尺寸（不含容差）
        placed_boxes: 已放置的箱子列表
        max_gap: 贴紧判定阈值（毫米），默认6.0
        pallet_dims: 可选托盘尺寸 {'length','width'}，提供时 x_max/y_max
            侧靠托盘边也算锚定；不提供时仅原点侧可按坐标判定靠边

    Returns:
        如果满足间隙约束返回True，否则返回False

    Notes:
        - 邻居判定使用 Z 区间重叠（而非 Z 坐标严格相等），覆盖跨层并排放置
          的箱子，避免漏判
        - 间隙按原始尺寸（raw dims）计算，尺寸容差产生的 2mm 名义间隙视为贴紧

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

    # 锚定判定：每个轴上，贴紧任一侧邻箱或托盘边即可；两侧均无邻箱不受约束。
    def _axis_anchored(low_gap, high_gap, low_edge, high_edge) -> bool:
        if low_gap is None and high_gap is None:
            return True  # 无有效邻箱，不受约束
        if low_gap is not None and low_gap < max_gap - eps:
            return True  # 贴紧低侧邻箱
        if high_gap is not None and high_gap < max_gap - eps:
            return True  # 贴紧高侧邻箱
        if low_edge is not None and low_edge < max_gap - eps:
            return True  # 推到低侧托盘边
        if high_edge is not None and high_edge < max_gap - eps:
            return True  # 推到高侧托盘边
        return False

    pallet_length = None
    pallet_width = None
    if pallet_dims:
        pallet_length = float(pallet_dims.get('length', 0) or 0) or None
        pallet_width = float(pallet_dims.get('width', 0) or 0) or None

    return _axis_anchored(
        nearest_gaps['x_min'], nearest_gaps['x_max'],
        x_min,
        (pallet_length - x_max) if pallet_length is not None else None,
    ) and _axis_anchored(
        nearest_gaps['y_min'], nearest_gaps['y_max'],
        y_min,
        (pallet_width - y_max) if pallet_width is not None else None,
    )


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
