"""
重叠检测函数

提供轴向重叠和XY平面重叠检测功能。
"""

from typing import Dict


def axis_overlap_len(a_min: float, a_max: float, b_min: float, b_max: float) -> float:
    """
    计算两个一维区间的重叠长度

    Args:
        a_min: 区间A的最小值
        a_max: 区间A的最大值
        b_min: 区间B的最小值
        b_max: 区间B的最大值

    Returns:
        重叠长度，如果不重叠则返回0.0

    Examples:
        >>> axis_overlap_len(0, 10, 5, 15)
        5.0
        >>> axis_overlap_len(0, 5, 10, 15)
        0.0
    """
    return max(0.0, min(a_max, b_max) - max(a_min, b_min))


def has_positive_xy_overlap(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_box: Dict,
    eps: float = 1e-9
) -> bool:
    """
    检查候选位置与已放置箱子在XY平面上是否有正重叠

    Args:
        point: 候选位置 {'x': float, 'y': float, 'z': float}
        dims: 候选箱子尺寸 {'length': float, 'width': float, 'height': float}
        placed_box: 已放置的箱子，包含 'position', 'length', 'width'
        eps: 数值容差

    Returns:
        如果在XY平面上有正重叠返回True，否则返回False

    Examples:
        >>> point = {'x': 0, 'y': 0, 'z': 0}
        >>> dims = {'length': 100, 'width': 100, 'height': 100}
        >>> placed = {
        ...     'position': {'x': 50, 'y': 50, 'z': 0},
        ...     'length': 100,
        ...     'width': 100
        ... }
        >>> has_positive_xy_overlap(point, dims, placed)
        True
    """
    placed_pos = placed_box.get('position')
    if not placed_pos:
        return False

    return (
        point['x'] < placed_pos['x'] + placed_box['length'] - eps and
        point['x'] + dims['length'] > placed_pos['x'] + eps and
        point['y'] < placed_pos['y'] + placed_box['width'] - eps and
        point['y'] + dims['width'] > placed_pos['y'] + eps
    )
