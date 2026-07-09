"""
几何计算模块

提供装箱系统所需的几何计算功能，包括重叠检测、支撑面积计算、间隙检查等。
"""

from .overlap import axis_overlap_len, has_positive_xy_overlap
from .support import calculate_direct_supported_area, direct_support_ratio
from .gap_checker import passes_box_gap_constraint, side_gap_flags, boundary_side_flags
from .center_of_mass import validate_center_of_mass, refresh_pallet_stability_status
from .constraint_validator import (
    validate_pallet_constraints,
    validate_plan_constraints,
)

__all__ = [
    # 重叠检测
    "axis_overlap_len",
    "has_positive_xy_overlap",
    # 支撑面积
    "calculate_direct_supported_area",
    "direct_support_ratio",
    # 间隙检查
    "passes_box_gap_constraint",
    "side_gap_flags",
    "boundary_side_flags",
    # 重心验证
    "validate_center_of_mass",
    "refresh_pallet_stability_status",
    "validate_pallet_constraints",
    "validate_plan_constraints",
]
