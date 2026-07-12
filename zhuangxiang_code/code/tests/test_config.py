"""
配置模块测试

验证配置加载和参数提取的正确性。
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.config import (
    PALLET_INDEX_TARGETS,
    MAX_BOX_GAP_MM,
    PROJECT_ROOT,
    DATA_DIR,
    OUTPUT_DIR,
    PalletConfig,
    PackingAlgorithmConfig,
    RobotSuctionConfig,
    RescueConfig,
    SmallBoxDetectionConfig,
    ExcelDataConfig,
)
from src.config.loader import ConfigLoader


def test_constants():
    """测试常量提取"""
    print("=" * 60)
    print("测试常量提取")
    print("=" * 60)

    print(f"PALLET_INDEX_TARGETS: {PALLET_INDEX_TARGETS}")
    assert PALLET_INDEX_TARGETS == {"MH423C": 192, "MH110": 32}

    print(f"MAX_BOX_GAP_MM: {MAX_BOX_GAP_MM}")
    assert MAX_BOX_GAP_MM == 6.0

    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"DATA_DIR: {DATA_DIR}")
    print(f"OUTPUT_DIR: {OUTPUT_DIR}")

    print("[PASS] 常量测试通过\n")


def test_pallet_config():
    """测试托盘配置"""
    print("=" * 60)
    print("测试托盘配置")
    print("=" * 60)

    config = PalletConfig(
        pallet_type="MH423C",
        length=1200.0,
        width=1000.0,
        height=1450.0,
        target_index=192
    )

    print(f"托盘类型: {config.pallet_type}")
    print(f"尺寸: {config.length} x {config.width} x {config.height}")
    print(f"目标指数: {config.target_index}")

    dims = config.to_dims_dict()
    print(f"尺寸字典: {dims}")
    assert dims == {'length': 1200.0, 'width': 1000.0, 'height': 1450.0}

    print("[PASS] 托盘配置测试通过\n")


def test_algorithm_config():
    """测试算法配置"""
    print("=" * 60)
    print("测试算法配置")
    print("=" * 60)

    # 测试默认值
    config = PackingAlgorithmConfig()
    print(f"默认配置: {config}")
    assert config.support_ratio_threshold == 0.8
    assert config.xy_tolerance == 2.0
    assert config.num_restarts == 30

    # 测试从字典创建
    custom_config = PackingAlgorithmConfig.from_dict({
        'num_restarts': 50,
        'beam_width': 10
    })
    print(f"自定义配置: {custom_config}")
    assert custom_config.num_restarts == 50
    assert custom_config.beam_width == 10
    assert custom_config.support_ratio_threshold == 0.8  # 使用默认值

    print("[PASS] 算法配置测试通过\n")


def test_robot_config():
    """测试机器人配置"""
    print("=" * 60)
    print("测试机器人配置")
    print("=" * 60)

    config = RobotSuctionConfig()
    print(f"机器人配置: {config}")
    assert config.cup_length == 600.0
    assert config.cup_width == 800.0
    assert config.allow_rotation_90 is True

    print("[PASS] 机器人配置测试通过\n")


def test_rescue_config():
    """测试救援配置"""
    print("=" * 60)
    print("测试救援配置")
    print("=" * 60)

    config = RescueConfig()
    print(f"救援配置: {config}")
    assert config.max_gap == 64.0
    assert config.max_attempts == 80
    assert config.topup_max_donor_scan == 80

    print("[PASS] 救援配置测试通过\n")


def test_config_loader():
    """测试配置加载器"""
    print("=" * 60)
    print("测试配置加载器")
    print("=" * 60)

    # 测试从YAML加载
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        loader = ConfigLoader(config_path)

        pallet_config = loader.load_pallet_config("MH423C")
        print(f"从YAML加载托盘配置: {pallet_config}")
        assert pallet_config.target_index == 192

        algo_config = loader.load_algorithm_config()
        print(f"从YAML加载算法配置: {algo_config}")
        assert algo_config.num_restarts == 30

        print("[PASS] YAML配置加载测试通过")
    else:
        print("[WARN] config.yaml 不存在，跳过YAML加载测试")

    # 测试默认配置
    default_loader = ConfigLoader.default()
    algo_config = default_loader.load_algorithm_config()
    print(f"默认算法配置: {algo_config}")
    assert algo_config.support_ratio_threshold == 0.8

    print("[PASS] 配置加载器测试通过\n")


def test_api_interface():
    """测试API接口（模拟前端传参）"""
    print("=" * 60)
    print("测试API接口（模拟前端传参）")
    print("=" * 60)

    # 模拟前端传入的配置
    frontend_config = {
        'algorithm': {
            'num_restarts': 20,
            'beam_width': 4,
            'support_ratio_threshold': 0.75
        },
        'robot': {
            'cup_length': 650.0,
            'allow_rotation_90': False
        },
        'rescue': {
            'max_gap': 50.0
        }
    }

    loader = ConfigLoader.from_dict(frontend_config)

    algo_config = loader.load_algorithm_config()
    print(f"前端传入算法配置: {algo_config}")
    assert algo_config.num_restarts == 20
    assert algo_config.beam_width == 4
    assert algo_config.support_ratio_threshold == 0.75

    robot_config = loader.load_robot_config()
    print(f"前端传入机器人配置: {robot_config}")
    assert robot_config.cup_length == 650.0
    assert robot_config.allow_rotation_90 is False
    assert robot_config.cup_width == 800.0  # 使用默认值

    rescue_config = loader.load_rescue_config()
    print(f"前端传入救援配置: {rescue_config}")
    assert rescue_config.max_gap == 50.0
    assert rescue_config.max_attempts == 80  # 使用默认值

    print("[PASS] API接口测试通过\n")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("配置模块测试套件")
    print("=" * 60 + "\n")

    try:
        test_constants()
        test_pallet_config()
        test_algorithm_config()
        test_robot_config()
        test_rescue_config()
        test_config_loader()
        test_api_interface()

        print("=" * 60)
        print("[PASS] 所有测试通过！")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] 测试出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
