"""
辅助函数

提供装箱系统所需的通用辅助功能。
"""

from typing import Dict, List
from copy import deepcopy


def has_box_above(item: Dict, items: List[Dict], eps: float = 1e-9) -> bool:
    """
    检查指定箱子上方是否有其他箱子

    Args:
        item: 要检查的箱子
        items: 所有箱子列表
        eps: 数值容差

    Returns:
        如果上方有箱子返回True，否则返回False

    Examples:
        >>> item = {
        ...     'id': 1,
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }
        >>> above = {
        ...     'id': 2,
        ...     'position': {'x': 0, 'y': 0, 'z': 100},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }
        >>> has_box_above(item, [item, above])
        True
    """
    item_pos = item.get('position')
    if not item_pos:
        return False

    item_top_z = item_pos['z'] + item.get('height', 0)
    item_dims = {'length': item.get('length', 0), 'width': item.get('width', 0)}

    for other in items:
        if other.get('id') == item.get('id'):
            continue

        other_pos = other.get('position')
        if not other_pos or other_pos['z'] + eps < item_top_z:
            continue

        # 检查XY平面是否有重叠
        if _has_positive_xy_overlap(item_pos, item_dims, other, eps=eps):
            return True

    return False


def _has_positive_xy_overlap(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_box: Dict,
    eps: float = 1e-9
) -> bool:
    """
    检查候选位置与已放置箱子在XY平面上是否有正重叠

    Args:
        point: 候选位置 {'x': float, 'y': float}
        dims: 候选箱子尺寸 {'length': float, 'width': float}
        placed_box: 已放置的箱子
        eps: 数值容差

    Returns:
        如果在XY平面上有正重叠返回True，否则返回False
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


def refresh_support_metrics(items: List[Dict]) -> None:
    """
    刷新所有箱子的支撑面积和支撑比例

    直接修改items列表中的箱子对象，添加或更新以下字段:
        - 'supported_area': 支撑面积（平方毫米）
        - 'support_ratio': 支撑比例（0.0到1.0）

    Args:
        items: 箱子列表，每个箱子必须包含 'position', 'length', 'width', 'height'

    Examples:
        >>> items = [
        ...     {
        ...         'position': {'x': 0, 'y': 0, 'z': 0},
        ...         'length': 100,
        ...         'width': 100,
        ...         'height': 100
        ...     },
        ...     {
        ...         'position': {'x': 0, 'y': 0, 'z': 100},
        ...         'length': 100,
        ...         'width': 100,
        ...         'height': 100
        ...     }
        ... ]
        >>> refresh_support_metrics(items)
        >>> items[0]['support_ratio']
        1.0
        >>> items[1]['support_ratio']
        1.0
    """
    # 延迟导入避免循环依赖
    from ..geometry.support import calculate_direct_supported_area

    for item in items:
        dims = {
            'length': float(item.get('length', 0) or 0),
            'width': float(item.get('width', 0) or 0),
            'height': float(item.get('height', 0) or 0),
        }
        supported_area = calculate_direct_supported_area(item['position'], dims, items)
        base_area = dims['length'] * dims['width']
        item['supported_area'] = float(supported_area)
        item['support_ratio'] = float(supported_area / base_area) if base_area > 0 else 0.0


def repack_ready_item(item: Dict) -> Dict:
    """
    将已放置的箱子转换为可重新装箱的状态

    移除位置、支撑信息和吸盘信息，恢复原始尺寸。

    Args:
        item: 已放置的箱子

    Returns:
        可重新装箱的箱子副本

    Examples:
        >>> placed_item = {
        ...     'id': 1,
        ...     'raw_length': 100,
        ...     'raw_width': 200,
        ...     'raw_height': 300,
        ...     'length': 102,
        ...     'width': 202,
        ...     'height': 300,
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'supported_area': 20000.0,
        ...     'support_ratio': 1.0,
        ...     'suction_box_corner': 'x_min_y_min'
        ... }
        >>> repack_item = repack_ready_item(placed_item)
        >>> 'position' in repack_item
        False
        >>> repack_item['length']
        100.0
    """
    from .dimensions import raw_dims as get_raw_dims

    item_copy = deepcopy(item)
    raw_dimensions = get_raw_dims(item_copy)

    # 恢复原始尺寸
    item_copy['length'] = raw_dimensions['length']
    item_copy['width'] = raw_dimensions['width']
    item_copy['height'] = raw_dimensions['height']

    # 移除放置相关信息
    item_copy.pop('position', None)
    item_copy.pop('supported_area', None)
    item_copy.pop('support_ratio', None)

    # 移除吸盘相关信息
    item_copy.pop('suction_box_corner', None)
    item_copy.pop('suction_cup_corner', None)
    item_copy.pop('suction_orientation', None)
    item_copy.pop('suction_cup_x_size', None)
    item_copy.pop('suction_cup_y_size', None)
    item_copy.pop('suction_rect_x_min', None)
    item_copy.pop('suction_rect_x_max', None)
    item_copy.pop('suction_rect_y_min', None)
    item_copy.pop('suction_rect_y_max', None)

    return item_copy


def sum_item_mpm(items):
    """快速求一组箱子的 min_pack_multiple 总和。"""
    return sum(
        float(item.get('min_pack_multiple', 0) or 0) for item in items
    )


def item_volume(item):
    """箱子有效体积（length * width * height）。"""
    return (
        float(item.get('length', 0) or 0)
        * float(item.get('width', 0) or 0)
        * float(item.get('height', 0) or 0)
    )


def passes_small_box_not_on_larger_constraint(item, point, dims, placed_boxes):
    """小箱不压大箱：若 item 被标记为小箱(is_small_box)且离地放置，其
    直接支撑层(顶面与本箱底面齐平且 XY 投影重叠的已放置箱)中不得有
    体积更大的箱子——防止较重的小箱压坏下方的大箱。

    地面箱(z<=0)与非小箱不受此约束。

    Args:
        item: 候选箱(需含 'is_small_box' 标记)
        point: 候选位置 {'x','y','z'}
        dims: 候选尺寸 {'length','width','height'}（用于体积与 XY 投影）
        placed_boxes: 已放置箱子列表

    Returns:
        True 表示通过；False 表示违例(小箱压在更大的箱子上)。
    """
    if not item.get('is_small_box', False):
        return True
    if point['z'] <= 1e-9:
        return True
    item_volume_value = (
        float(dims['length'])
        * float(dims['width'])
        * float(dims.get('height', 0) or 0)
    )
    for placed_box in placed_boxes:
        placed_pos = placed_box.get('position')
        if not placed_pos:
            continue
        placed_top_z = placed_pos['z'] + placed_box['height']
        if abs(placed_top_z - point['z']) > 1e-5:
            continue
        if not _has_positive_xy_overlap(point, dims, placed_box):
            continue
        placed_volume = (
            placed_box['length'] * placed_box['width'] * placed_box['height']
        )
        if placed_volume > item_volume_value + 1e-9:
            return False
    return True


def apply_suction_pose_fields(item_copy, suction_pose):
    """把 SuctionPlanner 返回的姿态字段写入 item 副本。"""
    item_copy['suction_box_corner'] = suction_pose['box_corner']
    item_copy['suction_cup_corner'] = suction_pose['cup_corner']
    item_copy['suction_orientation'] = suction_pose['orientation']
    item_copy['suction_cup_x_size'] = suction_pose['cup_x_size']
    item_copy['suction_cup_y_size'] = suction_pose['cup_y_size']
    item_copy['suction_rect_x_min'] = suction_pose['cup_rect']['x_min']
    item_copy['suction_rect_x_max'] = suction_pose['cup_rect']['x_max']
    item_copy['suction_rect_y_min'] = suction_pose['cup_rect']['y_min']
    item_copy['suction_rect_y_max'] = suction_pose['cup_rect']['y_max']
