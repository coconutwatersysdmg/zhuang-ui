"""
尺寸处理函数

提供箱子尺寸的提取和转换功能。
"""

from typing import Dict, Optional


def raw_dims(item: Dict, fallback_dims: Optional[Dict] = None) -> Dict[str, float]:
    """
    获取箱子的原始尺寸（不含容差）

    优先使用 raw_length/raw_width/raw_height，如果不存在则使用 length/width/height。

    Args:
        item: 箱子对象，可能包含以下键:
            - 'raw_length', 'raw_width', 'raw_height': 原始尺寸
            - 'length', 'width', 'height': 有效尺寸（可能包含容差）
        fallback_dims: 备用尺寸字典，当item中没有尺寸信息时使用

    Returns:
        原始尺寸字典 {'length': float, 'width': float, 'height': float}

    Examples:
        >>> item = {
        ...     'raw_length': 100,
        ...     'raw_width': 200,
        ...     'raw_height': 300,
        ...     'length': 102,
        ...     'width': 202,
        ...     'height': 300
        ... }
        >>> raw_dims(item)
        {'length': 100.0, 'width': 200.0, 'height': 300.0}

        >>> item = {'length': 100, 'width': 200, 'height': 300}
        >>> raw_dims(item)
        {'length': 100.0, 'width': 200.0, 'height': 300.0}
    """
    fallback_dims = fallback_dims or {}
    return {
        'length': float(
            item.get('raw_length', fallback_dims.get('length', item.get('length', 0))) or 0
        ),
        'width': float(
            item.get('raw_width', fallback_dims.get('width', item.get('width', 0))) or 0
        ),
        'height': float(
            item.get('raw_height', fallback_dims.get('height', item.get('height', 0))) or 0
        ),
    }


def effective_dims(
    raw_dims: Dict[str, float],
    xy_tolerance: float = 0.0,
    z_tolerance: float = 0.0
) -> Dict[str, float]:
    """
    计算箱子的有效尺寸（包含容差）

    Args:
        raw_dims: 原始尺寸 {'length': float, 'width': float, 'height': float}
        xy_tolerance: XY方向容差（毫米），默认0.0
        z_tolerance: Z方向容差（毫米），默认0.0

    Returns:
        有效尺寸字典 {'length': float, 'width': float, 'height': float}

    Examples:
        >>> dims = {'length': 100, 'width': 200, 'height': 300}
        >>> effective_dims(dims, xy_tolerance=2.0, z_tolerance=0.0)
        {'length': 102.0, 'width': 202.0, 'height': 300.0}
    """
    return {
        'length': float(raw_dims['length']) + xy_tolerance,
        'width': float(raw_dims['width']) + xy_tolerance,
        'height': float(raw_dims['height']) + z_tolerance,
    }
