"""
装箱模块

提供装箱算法的核心实现：候选点生成、放置验证、吸盘规划、Beam Search 装箱、
确定性整层装箱、装箱后清理。
"""

from .beam_search_packer import BeamSearchPacker
from .candidate_generator import CandidatePointGenerator
from .direct_layer_packer import (
    build_centered_single_box_solution,
    build_direct_layer_packing_solution,
)
from .layer_pool_builder import build_layer_aware_candidate_pool
from .placement_validator import PlacementValidator
from .sanitizer import sanitize_packed_items
from .pool_compactor import PoolCompactor
from .suction_planner import SuctionPlanner

__all__ = [
    "BeamSearchPacker",
    "CandidatePointGenerator",
    "PlacementValidator",
    "SuctionPlanner",
    "build_layer_aware_candidate_pool",
    "build_direct_layer_packing_solution",
    "build_centered_single_box_solution",
    "sanitize_packed_items",
    "PoolCompactor",
]
