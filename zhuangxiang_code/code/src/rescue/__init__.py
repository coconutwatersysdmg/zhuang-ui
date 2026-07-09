"""
救援模块

提供失败托盘的优化和救援功能：托盘评估、索引构建、合池重建、
快速补齐 / 洞填充 / 配方重建。
"""

from .failed_pool_rebuilder import FailedPoolRebuilder
from .hole_fill_rescuer import fast_rescue_failed_pallets_by_hole_fill
from .index_builder import IndexBuilder
from .low_load_rebuilder import LowLoadRebuilder
from .low_fill_repacker import LowFillRepacker
from .pallet_evaluator import PalletEvaluator
from .recipe_rebuilder import rescue_by_recipe_rebuild
from .rescue_optimizer import RescueOptimizer
from .tail_fragment_absorber import TailFragmentAbsorber
from .topup_rescuer import fast_rescue_failed_pallets_by_topup

__all__ = [
    "PalletEvaluator",
    "IndexBuilder",
    "RescueOptimizer",
    "FailedPoolRebuilder",
    "LowLoadRebuilder",
    "LowFillRepacker",
    "TailFragmentAbsorber",
    "fast_rescue_failed_pallets_by_topup",
    "fast_rescue_failed_pallets_by_hole_fill",
    "rescue_by_recipe_rebuild",
]
