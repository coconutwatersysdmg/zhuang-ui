"""支撑面积计算函数（纯数值版，等价替换原 Shapely 实现）。

底面支撑面积 = 候选箱底面矩形 与 "顶面齐平的支撑箱底面矩形之并集" 的交集面积。
因全部为轴对齐矩形，用坐标压缩 + 逐 x 切片求 y 覆盖长度精确计算，
与 shapely unary_union/intersection 数值等价（float64，差异 ~1e-9）。
"""
from typing import Dict, List, Tuple


def _union_area(rects: List[Tuple[float, float, float, float]]) -> float:
    """轴对齐矩形 (x0,x1,y0,y1) 列表的并集面积。

    注：四元组用 (x0,x1,y0,y1)（两个 x 在前）而非 shapely 惯用的
    (x0,y0,x1,y1)，便于下面按 x 切片分组。
    """
    # 坐标压缩：用所有矩形的 x 边界把 x 轴切成若干相邻切片。
    # 不变量：任一矩形对某个切片要么完全覆盖、要么完全不相交（边界都在 xs 中）。
    xs = sorted({r[0] for r in rects} | {r[1] for r in rects})
    total = 0.0
    for i in range(len(xs) - 1):
        x_left = xs[i]
        x_right = xs[i + 1]
        width = x_right - x_left
        if width <= 0:
            continue
        # 选出覆盖整个切片的矩形，取它们的 y 区间
        intervals = [
            (r[2], r[3]) for r in rects
            if r[0] <= x_left and r[1] >= x_right
        ]
        if not intervals:
            continue
        # y 区间排序后线性合并，cov = 该切片内被覆盖的 y 总长
        intervals.sort()
        cov = 0.0
        cur_lo, cur_hi = intervals[0]
        for lo, hi in intervals[1:]:
            if lo > cur_hi:
                cov += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
            elif hi > cur_hi:
                cur_hi = hi
        cov += cur_hi - cur_lo
        total += cov * width
    return total


def calculate_direct_supported_area(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_boxes: List[Dict],
) -> float:
    """计算箱子在指定位置的直接支撑面积（纯数值，等价于原 Shapely 实现）。

    Notes:
        - 箱子在地面上（z==0）返回底面积。
        - 无齐平支撑箱返回 0.0。
        - 用 1e-5 容差判断顶面与候选底面齐平。
    """
    if point['z'] == 0:
        return dims['length'] * dims['width']

    ux0 = point['x']
    uy0 = point['y']
    ux1 = ux0 + dims['length']
    uy1 = uy0 + dims['width']
    if ux1 <= ux0 or uy1 <= uy0:
        return 0.0

    pz = point['z']
    clipped: List[Tuple[float, float, float, float]] = []
    for box in placed_boxes:
        bpos = box['position']
        if abs((bpos['z'] + box['height']) - pz) >= 1e-5:
            continue
        bx0 = bpos['x']
        by0 = bpos['y']
        bx1 = bx0 + box['length']
        by1 = by0 + box['width']
        ix0 = bx0 if bx0 > ux0 else ux0
        ix1 = bx1 if bx1 < ux1 else ux1
        iy0 = by0 if by0 > uy0 else uy0
        iy1 = by1 if by1 < uy1 else uy1
        if ix1 > ix0 and iy1 > iy0:
            clipped.append((ix0, ix1, iy0, iy1))

    if not clipped:
        return 0.0
    return _union_area(clipped)


def direct_support_ratio(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_boxes: List[Dict]
) -> float:
    """
    计算箱子在指定位置的支撑比例

    支撑比例 = 支撑面积 / 箱子底面积

    Args:
        point: 箱子位置 {'x': float, 'y': float, 'z': float}
        dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}
        placed_boxes: 已放置的箱子列表

    Returns:
        支撑比例（0.0 到 1.0）

    Examples:
        >>> point = {'x': 0, 'y': 0, 'z': 100}
        >>> dims = {'length': 100, 'width': 100, 'height': 100}
        >>> placed = [{
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }]
        >>> direct_support_ratio(point, dims, placed)
        1.0
    """
    base_area = dims['length'] * dims['width']
    if base_area <= 0:
        return 0.0
    return calculate_direct_supported_area(point, dims, placed_boxes) / base_area
