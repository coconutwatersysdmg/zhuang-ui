"""
工具函数模块

提供装箱系统所需的通用工具函数。
"""

from .dimensions import raw_dims, effective_dims
from .helpers import has_box_above, refresh_support_metrics, repack_ready_item

__all__ = [
    # 尺寸处理
    "raw_dims",
    "effective_dims",
    # 辅助函数
    "has_box_above",
    "refresh_support_metrics",
    "repack_ready_item",
]
