"""
工具函数模块

提供装箱系统所需的通用工具函数。
"""

from .case_group import (
    find_case_group_violation,
    normalize_case_group,
    split_case_group_tag,
    tag_sales_order_no,
)
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
    # case_group 同组约束
    "normalize_case_group",
    "find_case_group_violation",
    "tag_sales_order_no",
    "split_case_group_tag",
]
