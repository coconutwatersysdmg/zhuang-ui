"""
配置模块使用示例

演示如何在实际代码中使用配置模块。
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from src.config import (
    PALLET_INDEX_TARGETS,
    MAX_BOX_GAP_MM,
    DATA_DIR,
    OUTPUT_DIR,
)
from src.config.loader import ConfigLoader


def example_1_use_constants():
    """示例1: 使用全局常量"""
    print("=" * 60)
    print("示例1: 使用全局常量")
    print("=" * 60)

    print(f"托盘指数目标: {PALLET_INDEX_TARGETS}")
    print(f"箱间最大间隙: {MAX_BOX_GAP_MM} mm")
    print(f"数据目录: {DATA_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print()


def example_2_load_from_yaml():
    """示例2: 从YAML文件加载配置"""
    print("=" * 60)
    print("示例2: 从YAML文件加载配置")
    print("=" * 60)

    config_path = project_root / "config.yaml"
    loader = ConfigLoader(config_path)

    # 加载托盘配置
    pallet_config = loader.load_pallet_config("MH423C")
    print(f"托盘类型: {pallet_config.pallet_type}")
    print(f"托盘尺寸: {pallet_config.length} x {pallet_config.width} x {pallet_config.height}")
    print(f"目标指数: {pallet_config.target_index}")

    # 加载算法配置
    algo_config = loader.load_algorithm_config()
    print(f"\n算法参数:")
    print(f"  - 支撑比例阈值: {algo_config.support_ratio_threshold}")
    print(f"  - XY容差: {algo_config.xy_tolerance} mm")
    print(f"  - 重启次数: {algo_config.num_restarts}")
    print(f"  - Beam宽度: {algo_config.beam_width}")

    # 加载机器人配置
    robot_config = loader.load_robot_config()
    print(f"\n机器人参数:")
    print(f"  - 吸盘尺寸: {robot_config.cup_length} x {robot_config.cup_width} mm")
    print(f"  - 允许旋转: {robot_config.allow_rotation_90}")
    print()


def example_3_api_interface():
    """示例3: API接口（模拟前端传参）"""
    print("=" * 60)
    print("示例3: API接口（模拟前端传参）")
    print("=" * 60)

    # 模拟前端只传入需要修改的参数
    frontend_params = {
        'algorithm': {
            'num_restarts': 50,  # 修改重启次数
            'beam_width': 10,    # 修改Beam宽度
        },
        'robot': {
            'cup_length': 700.0,  # 修改吸盘长度
        }
        # 其他参数使用默认值
    }

    loader = ConfigLoader.from_dict(frontend_params)

    algo_config = loader.load_algorithm_config()
    print(f"算法参数:")
    print(f"  - 重启次数: {algo_config.num_restarts} (前端传入)")
    print(f"  - Beam宽度: {algo_config.beam_width} (前端传入)")
    print(f"  - 支撑比例阈值: {algo_config.support_ratio_threshold} (使用默认值)")
    print(f"  - XY容差: {algo_config.xy_tolerance} (使用默认值)")

    robot_config = loader.load_robot_config()
    print(f"\n机器人参数:")
    print(f"  - 吸盘长度: {robot_config.cup_length} (前端传入)")
    print(f"  - 吸盘宽度: {robot_config.cup_width} (使用默认值)")
    print()


def example_4_use_default():
    """示例4: 使用默认配置"""
    print("=" * 60)
    print("示例4: 使用默认配置")
    print("=" * 60)

    loader = ConfigLoader.default()

    algo_config = loader.load_algorithm_config()
    print(f"算法参数 (全部使用默认值):")
    print(f"  - 支撑比例阈值: {algo_config.support_ratio_threshold}")
    print(f"  - XY容差: {algo_config.xy_tolerance} mm")
    print(f"  - Z容差: {algo_config.z_tolerance} mm")
    print(f"  - 重启次数: {algo_config.num_restarts}")
    print(f"  - Beam宽度: {algo_config.beam_width}")
    print(f"  - 候选限制: {algo_config.candidate_limit}")

    rescue_config = loader.load_rescue_config()
    print(f"\n救援参数 (全部使用默认值):")
    print(f"  - 最大缺口: {rescue_config.max_gap}")
    print(f"  - 最大尝试次数: {rescue_config.max_attempts}")
    print(f"  - 补齐扫描数: {rescue_config.topup_max_donor_scan}")
    print(f"  - 补洞扫描数: {rescue_config.hole_fill_max_donor_scan}")
    print()


def example_5_pallet_dims_dict():
    """示例5: 转换为原代码兼容的字典格式"""
    print("=" * 60)
    print("示例5: 转换为原代码兼容的字典格式")
    print("=" * 60)

    config_path = project_root / "config.yaml"
    loader = ConfigLoader(config_path)
    pallet_config = loader.load_pallet_config("MH423C")

    # 转换为原代码使用的字典格式
    pallet_dims = pallet_config.to_dims_dict()
    print(f"托盘尺寸字典 (兼容原代码): {pallet_dims}")

    # 可以直接传给原代码的函数
    print(f"托盘长度: {pallet_dims['length']}")
    print(f"托盘宽度: {pallet_dims['width']}")
    print(f"托盘高度: {pallet_dims['height']}")
    print()


def example_6_complete_workflow():
    """示例6: 完整工作流（模拟实际使用）"""
    print("=" * 60)
    print("示例6: 完整工作流（模拟实际使用）")
    print("=" * 60)

    # 步骤1: 前端传入配置
    frontend_config = {
        'algorithm': {
            'num_restarts': 40,
            'beam_width': 8,
            'support_ratio_threshold': 0.85
        },
        'robot': {
            'cup_length': 650.0,
            'cup_width': 850.0,
            'allow_rotation_90': True
        },
        'rescue': {
            'max_gap': 70.0,
            'max_attempts': 100
        }
    }

    # 步骤2: 加载配置
    loader = ConfigLoader.from_dict(frontend_config)
    algo_config = loader.load_algorithm_config()
    robot_config = loader.load_robot_config()
    rescue_config = loader.load_rescue_config()

    # 步骤3: 使用配置（模拟装箱流程）
    print("装箱系统初始化...")
    print(f"  算法配置: 重启{algo_config.num_restarts}次, Beam宽度{algo_config.beam_width}")
    print(f"  机器人配置: 吸盘{robot_config.cup_length}x{robot_config.cup_width}mm")
    print(f"  救援配置: 最大缺口{rescue_config.max_gap}, 最大尝试{rescue_config.max_attempts}次")

    print("\n开始装箱...")
    print(f"  使用支撑比例阈值: {algo_config.support_ratio_threshold}")
    print(f"  使用XY容差: {algo_config.xy_tolerance} mm")
    print(f"  使用箱间最大间隙: {MAX_BOX_GAP_MM} mm")

    print("\n装箱完成！")
    print()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("配置模块使用示例")
    print("=" * 60 + "\n")

    example_1_use_constants()
    example_2_load_from_yaml()
    example_3_api_interface()
    example_4_use_default()
    example_5_pallet_dims_dict()
    example_6_complete_workflow()

    print("=" * 60)
    print("所有示例运行完成！")
    print("=" * 60)
