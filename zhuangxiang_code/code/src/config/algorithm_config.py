"""
算法参数配置类

定义装箱算法、机器人约束、救援策略等所有可配置参数。
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class PackingAlgorithmConfig:
    """
    装箱算法核心参数

    Attributes:
        support_ratio_threshold: 支撑面积比例阈值（默认 0.8）
        xy_tolerance: XY方向尺寸容差（毫米，默认 2.0）
        z_tolerance: Z方向尺寸容差（毫米，默认 0.0）
        num_restarts: 多起点搜索次数（默认 30）
        beam_width: Beam Search 宽度（默认 6）
        candidate_limit: 每个状态保留的候选放置数量上限（默认 30）
        max_candidate_points: 最大候选点数量（默认 200）
        max_points_per_layer: 每层最大候选点数量（默认 40）
    """
    support_ratio_threshold: float = 0.8
    xy_tolerance: float = 2.0
    z_tolerance: float = 0.0
    num_restarts: int = 30
    beam_width: int = 6
    candidate_limit: int = 30
    max_candidate_points: int = 200
    max_points_per_layer: int = 40

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'PackingAlgorithmConfig':
        """从字典创建配置对象，未提供的参数使用默认值"""
        if data is None:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass(frozen=True)
class RobotSuctionConfig:
    """
    机器人吸盘配置

    Attributes:
        cup_length: 吸盘长度（毫米，默认 600.0）
        cup_width: 吸盘宽度（毫米，默认 800.0）
        xy_clearance: XY方向安全间隙（毫米，默认 0.0）
        z_clearance: Z方向安全间隙（毫米，默认 0.0）
        allow_rotation_90: 是否允许吸盘旋转90度（默认 True）
        reachability_enabled: 是否启用机器人可达性检查（默认 True）
    """
    cup_length: float = 600.0
    cup_width: float = 800.0
    xy_clearance: float = 0.0
    z_clearance: float = 0.0
    allow_rotation_90: bool = True
    reachability_enabled: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'RobotSuctionConfig':
        """从字典创建配置对象，未提供的参数使用默认值"""
        if data is None:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass(frozen=True)
class RescueConfig:
    """
    失败托盘救援策略参数

    Attributes:
        max_gap: 最大可救援的指数缺口（默认 64.0）
        max_attempts: 最大救援尝试次数（默认 80）
        topup_max_donor_scan: 补齐策略最大扫描捐赠箱子数（默认 80）
        topup_donor_mpm_slack: 补齐策略捐赠者指数松弛量（默认 16.0）
        hole_fill_max_donor_scan: 补洞策略最大扫描捐赠箱子数（默认 160）
        hole_fill_max_add_items: 补洞策略最大添加箱子数（默认 8）
        recipe_max_group_boxes: 配方重建最大箱子组大小（默认 400）
        recipe_max_count: 配方重建最大配方数量（默认 12）
        seed_base_topup: 补齐策略随机种子基数（默认 31000）
        seed_base_hole_fill: 补洞策略随机种子基数（默认 41000）
        seed_base_recipe: 配方重建随机种子基数（默认 51000）
    """
    max_gap: float = 64.0
    max_attempts: int = 80
    topup_max_donor_scan: int = 80
    topup_donor_mpm_slack: float = 16.0
    hole_fill_max_donor_scan: int = 160
    hole_fill_max_add_items: int = 8
    recipe_max_group_boxes: int = 400
    recipe_max_count: int = 12
    seed_base_topup: int = 31000
    seed_base_hole_fill: int = 41000
    seed_base_recipe: int = 51000

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'RescueConfig':
        """从字典创建配置对象，未提供的参数使用默认值"""
        if data is None:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass(frozen=True)
class SmallBoxDetectionConfig:
    """
    小箱子检测参数

    Attributes:
        source_file: 数据源文件名（相对于 DATA_DIR）
        source_sheet: 结果数据表名
        bms_sheet: BMS数据表名
        index_smooth_window: 指数平滑窗口大小（默认 5）
        index_plateau_window: 平台检测窗口大小（默认 6）
        index_plateau_rel_tol: 平台相对容差（默认 0.02）
        index_plateau_abs_tol: 平台绝对容差（默认 0.8）
        index_min_slope_window: 最小斜率窗口大小（默认 3）
        index_near_peak_gap: 峰值附近间隙比例（默认 0.15）
    """
    source_file: str = "多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx"
    source_sheet: str = "最终挑选结果"
    bms_sheet: str = "包装物料主数据(BMS)"
    index_smooth_window: int = 5
    index_plateau_window: int = 6
    index_plateau_rel_tol: float = 0.02
    index_plateau_abs_tol: float = 0.8
    index_min_slope_window: int = 3
    index_near_peak_gap: float = 0.15

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'SmallBoxDetectionConfig':
        """从字典创建配置对象，未提供的参数使用默认值"""
        if data is None:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass(frozen=True)
class ExcelDataConfig:
    """
    Excel数据加载配置

    Attributes:
        source_file: 数据源文件名（相对于 DATA_DIR）
        result_sheet: 结果数据表名
        bms_sheet: BMS数据表名
        required_columns: 必需的列名列表
    """
    source_file: str = "多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx"
    result_sheet: str = "最终挑选结果"
    bms_sheet: str = "包装物料主数据(BMS)"
    required_columns: tuple = (
        '箱子序号', 'Box类型', '总重量', '箱子长', '箱子宽', '箱子高',
        'Case类型', '销售订单号', '托盘长', '托盘宽', '托盘高'
    )

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'ExcelDataConfig':
        """从字典创建配置对象，未提供的参数使用默认值"""
        if data is None:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})
