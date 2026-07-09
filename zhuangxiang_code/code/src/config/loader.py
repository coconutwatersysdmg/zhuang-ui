"""
配置加载器

支持从YAML文件或字典加载配置，提供统一的配置管理接口。
"""

from pathlib import Path
from typing import Dict, Optional, Any
import yaml

from .constants import DATA_DIR, OUTPUT_DIR, PALLET_INDEX_TARGETS
from .pallet_config import PalletConfig
from .constraint_config import ConstraintConfig
from .algorithm_config import (
    PackingAlgorithmConfig,
    RobotSuctionConfig,
    RescueConfig,
    SmallBoxDetectionConfig,
    ExcelDataConfig,
)


class ConfigLoader:
    """
    配置加载器

    支持从YAML文件或字典加载所有配置参数。
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置加载器

        Args:
            config_path: YAML配置文件路径（可选）
        """
        self.config_data = {}
        if config_path and config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config_data = yaml.safe_load(f) or {}

    def load_pallet_config(self, pallet_type: str) -> PalletConfig:
        """
        加载托盘配置

        Args:
            pallet_type: 托盘类型代码

        Returns:
            PalletConfig 实例

        Raises:
            ValueError: 如果托盘类型未配置
        """
        pallet_data = self.config_data.get('pallets', {}).get(pallet_type)
        if not pallet_data:
            raise ValueError(f"托盘类型 '{pallet_type}' 未在配置中找到")

        return PalletConfig(
            pallet_type=pallet_type,
            length=float(pallet_data['length']),
            width=float(pallet_data['width']),
            height=float(pallet_data['height']),
            target_index=float(pallet_data.get('target_index', PALLET_INDEX_TARGETS.get(pallet_type, 0)))
        )

    def load_constraint_config(self) -> ConstraintConfig:
        """加载装箱约束统一配置。

        从 YAML 的 ``constraints`` 段读取（开关 + 间隙/支撑率/重心数值 +
        吸盘几何）。为兼容旧模板，吸盘几何也接受来自 ``robot`` 段的字段，
        ``constraints`` 段优先级更高。
        """
        constraints = dict(self.config_data.get('constraints') or {})

        # 兼容旧 robot 段的吸盘几何：仅在 constraints 未覆盖时回填
        robot = self.config_data.get('robot') or {}
        robot_alias = {
            'cup_length': 'suction_cup_length',
            'cup_width': 'suction_cup_width',
            'xy_clearance': 'suction_xy_clearance',
            'z_clearance': 'suction_z_clearance',
            'allow_rotation_90': 'suction_allow_rotation_90',
            'reachability_enabled': 'suction_reachability_enabled',
        }
        for old_key, new_key in robot_alias.items():
            if new_key not in constraints and old_key in robot:
                constraints[new_key] = robot[old_key]

        return ConstraintConfig.from_dict(constraints)

    def load_algorithm_config(self) -> PackingAlgorithmConfig:
        """加载装箱算法配置"""
        return PackingAlgorithmConfig.from_dict(
            self.config_data.get('algorithm')
        )

    def load_robot_config(self) -> RobotSuctionConfig:
        """加载机器人吸盘配置"""
        return RobotSuctionConfig.from_dict(
            self.config_data.get('robot')
        )

    def load_rescue_config(self) -> RescueConfig:
        """加载救援策略配置"""
        return RescueConfig.from_dict(
            self.config_data.get('rescue')
        )

    def load_small_box_detection_config(self) -> SmallBoxDetectionConfig:
        """加载小箱子检测配置"""
        return SmallBoxDetectionConfig.from_dict(
            self.config_data.get('small_box_detection')
        )

    def load_excel_config(self) -> ExcelDataConfig:
        """加载Excel数据配置"""
        return ExcelDataConfig.from_dict(
            self.config_data.get('excel_data')
        )

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'ConfigLoader':
        """
        从字典创建配置加载器（用于API传入参数）

        Args:
            config_dict: 配置字典

        Returns:
            ConfigLoader 实例
        """
        loader = cls()
        loader.config_data = config_dict
        return loader

    @classmethod
    def default(cls) -> 'ConfigLoader':
        """
        创建使用默认值的配置加载器

        Returns:
            ConfigLoader 实例
        """
        return cls()


def create_default_config_yaml(output_path: Path) -> None:
    """
    创建默认配置YAML文件模板

    Args:
        output_path: 输出文件路径
    """
    default_config = {
        'pallets': {
            'MH423C': {
                'length': 1200.0,
                'width': 1000.0,
                'height': 1450.0,
                'target_index': 192
            },
            'MH110': {
                'length': 1200.0,
                'width': 800.0,
                'height': 1450.0,
                'target_index': 32
            }
        },
        'constraints': {
            # —— 主装箱算法选择 ——
            'main_packer': 'gcp',
            # —— 必须约束的可配数值（约束本身不可关闭）——
            'max_box_gap_mm': 6.0,
            'support_ratio_threshold': 0.8,
            'center_of_mass_tolerance': 1.0 / 3.0,
            # —— 可关约束开关（默认 True，可改 False 关闭）——
            'suction_reachability_enabled': True,
            'small_box_below_enabled': True,
            'same_size_heavier_below_enabled': True,
            'height_multiple_layering_enabled': True,
            # —— 吸盘几何（仅 suction_reachability_enabled=True 时生效）——
            'suction_cup_length': 600.0,
            'suction_cup_width': 800.0,
            'suction_xy_clearance': 0.0,
            'suction_z_clearance': 0.0,
            'suction_allow_rotation_90': True,
            # —— baseline 箱子 90° 旋转（朝向规整，带几何门槛；默认 True）——
            'allow_box_rotation_90': True,
        },
        'tolerances': {
            'xy_tolerance': 2.0,
            'z_tolerance': 0.0
        },
        'algorithm': {
            'num_restarts': 30,
            'beam_width': 6,
            'candidate_limit': 30,
            'max_candidate_points': 200,
            'max_points_per_layer': 40
        },
        'rescue': {
            'max_gap': 64.0,
            'max_attempts': 80,
            'topup_max_donor_scan': 80,
            'hole_fill_max_donor_scan': 160,
            'hole_fill_max_add_items': 8,
            'recipe_max_group_boxes': 400,
            'recipe_max_count': 12
        },
        'small_box_detection': {
            'index_smooth_window': 5,
            'index_plateau_window': 6,
            'index_plateau_rel_tol': 0.02,
            'index_plateau_abs_tol': 0.8,
            'index_min_slope_window': 3,
            'index_near_peak_gap': 0.15
        },
        'excel_data': {
            'source_file': '多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx',
            'result_sheet': '最终挑选结果',
            'bms_sheet': '包装物料主数据(BMS)'
        }
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(default_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
