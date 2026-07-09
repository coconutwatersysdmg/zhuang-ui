"""
重心验证函数

提供托盘装载方案的重心位置验证功能。
"""

from typing import Dict, List


def validate_center_of_mass(
    pallet_plan: Dict,
    pallet_dims: Dict[str, float],
    tolerance: float = 1/3
) -> Dict:
    """
    验证托盘装载方案的重心位置是否在允许范围内

    通过计算所有装载物品的合力矩来确定整体重心位置，然后与托盘几何中心进行比较。
    如果重心在X或Y方向的偏移量超过托盘对应方向尺寸的tolerance比例，则判定为不稳定。

    Args:
        pallet_plan: 托盘装载方案，包含 'packed_items' 键，值为已装载物品的列表
        pallet_dims: 托盘尺寸信息，包含以下键:
            - 'length' (float): 托盘长度
            - 'width' (float): 托盘宽度
        tolerance: 重心偏移容忍度，默认为 1/3，表示重心相对于托盘中心的最大允许偏移比例

    Returns:
        验证结果字典，包含以下键:
            - 'is_stable' (bool): 重心是否稳定，True表示在允许范围内，False表示超出容忍度
            - 'center_of_mass' (dict): 重心坐标 {'x': float, 'y': float}
            - 'pallet_center' (dict): 托盘中心坐标 {'x': float, 'y': float}
            - 'offset_x' (float): X方向偏移量（毫米）
            - 'offset_y' (float): Y方向偏移量（毫米）
            - 'offset_x_percent' (float, 可选): X方向偏移百分比，仅在不稳定时返回
            - 'offset_y_percent' (float, 可选): Y方向偏移百分比，仅在不稳定时返回

    Examples:
        >>> pallet_plan = {
        ...     'packed_items': [
        ...         {
        ...             'position': {'x': 0, 'y': 0, 'z': 0},
        ...             'length': 100,
        ...             'width': 100,
        ...             'height': 100,
        ...             'weight': 10.0
        ...         }
        ...     ]
        ... }
        >>> pallet_dims = {'length': 1200, 'width': 1000}
        >>> result = validate_center_of_mass(pallet_plan, pallet_dims)
        >>> result['is_stable']
        True
    """
    total_weight = 0.0
    moment_x = 0.0
    moment_y = 0.0

    for box in pallet_plan['packed_items']:
        weight = float(box.get('weight', 0.0) or 0.0)
        pos = box['position']
        dim = box

        # 计算每个物品在X和Y方向的重心坐标（基于物品几何中心）
        cx = pos['x'] + dim['length'] / 2
        cy = pos['y'] + dim['width'] / 2

        # 累加总重量和对各轴的力矩
        total_weight += weight
        moment_x += weight * cx
        moment_y += weight * cy

    # 如果总重量为0，无法计算重心，默认为稳定
    if total_weight == 0:
        return {
            'is_stable': True,
            'center_of_mass': {'x': 0.0, 'y': 0.0},
            'pallet_center': {
                'x': float(pallet_dims['length']) / 2,
                'y': float(pallet_dims['width']) / 2
            },
            'offset_x': 0.0,
            'offset_y': 0.0,
        }

    # 计算重心坐标
    com_x = moment_x / total_weight
    com_y = moment_y / total_weight

    # 计算托盘几何中心
    pallet_center_x = float(pallet_dims['length']) / 2
    pallet_center_y = float(pallet_dims['width']) / 2

    # 计算重心偏移量
    offset_x = com_x - pallet_center_x
    offset_y = com_y - pallet_center_y

    # 计算允许的最大偏移量
    max_offset_x = float(pallet_dims['length']) * tolerance
    max_offset_y = float(pallet_dims['width']) * tolerance

    # 判断是否稳定
    is_stable = (
        abs(offset_x) <= max_offset_x and
        abs(offset_y) <= max_offset_y
    )

    result = {
        'is_stable': is_stable,
        'center_of_mass': {'x': com_x, 'y': com_y},
        'pallet_center': {'x': pallet_center_x, 'y': pallet_center_y},
        'offset_x': offset_x,
        'offset_y': offset_y,
    }

    # 如果不稳定，添加偏移百分比信息
    if not is_stable:
        result['offset_x_percent'] = abs(offset_x) / float(pallet_dims['length']) * 100
        result['offset_y_percent'] = abs(offset_y) / float(pallet_dims['width']) * 100

    return result


def refresh_pallet_stability_status(
    solution: dict, pallet_dims: dict, tolerance: float = 1.0 / 3.0
) -> str:
    """计算并写回托盘的整体稳定性字段。

    直接修改 solution，添加 stability_checks 字段（含 status / 可选的
    center_of_mass_failure），返回 status 字符串。

    tolerance 与门禁 validate_pallet_constraints 的重心偏差阈值同源；
    默认 1/3 与历史行为一致。
    """
    issues = validate_center_of_mass(solution, pallet_dims, tolerance=tolerance)
    stability_checks = {}
    if not issues.get('is_stable', False):
        stability_checks['center_of_mass_failure'] = issues
        stability_checks['status'] = 'FAILED'
    else:
        stability_checks['status'] = 'SUCCESS'
    solution['stability_checks'] = stability_checks
    return stability_checks['status']

