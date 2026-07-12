"""
工具模块测试

验证工具函数的正确性。
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.utils import (
    raw_dims,
    effective_dims,
    has_box_above,
    refresh_support_metrics,
    repack_ready_item,
)


def test_raw_dims():
    """测试原始尺寸提取"""
    print("=" * 60)
    print("测试原始尺寸提取")
    print("=" * 60)

    # 测试有raw_*字段
    item = {
        'raw_length': 100,
        'raw_width': 200,
        'raw_height': 300,
        'length': 102,
        'width': 202,
        'height': 300
    }
    dims = raw_dims(item)
    print(f"有raw_*字段的箱子原始尺寸: {dims}")
    assert dims == {'length': 100.0, 'width': 200.0, 'height': 300.0}

    # 测试无raw_*字段
    item = {'length': 100, 'width': 200, 'height': 300}
    dims = raw_dims(item)
    print(f"无raw_*字段的箱子原始尺寸: {dims}")
    assert dims == {'length': 100.0, 'width': 200.0, 'height': 300.0}

    # 测试使用fallback
    item = {}
    fallback = {'length': 50, 'width': 60, 'height': 70}
    dims = raw_dims(item, fallback_dims=fallback)
    print(f"使用fallback的箱子原始尺寸: {dims}")
    assert dims == {'length': 50.0, 'width': 60.0, 'height': 70.0}

    print("[PASS] 原始尺寸提取测试通过\n")


def test_effective_dims():
    """测试有效尺寸计算"""
    print("=" * 60)
    print("测试有效尺寸计算")
    print("=" * 60)

    # 测试无容差
    dims = {'length': 100, 'width': 200, 'height': 300}
    eff_dims = effective_dims(dims, xy_tolerance=0.0, z_tolerance=0.0)
    print(f"无容差的有效尺寸: {eff_dims}")
    assert eff_dims == {'length': 100.0, 'width': 200.0, 'height': 300.0}

    # 测试有容差
    dims = {'length': 100, 'width': 200, 'height': 300}
    eff_dims = effective_dims(dims, xy_tolerance=2.0, z_tolerance=0.0)
    print(f"有容差的有效尺寸: {eff_dims}")
    assert eff_dims == {'length': 102.0, 'width': 202.0, 'height': 300.0}

    print("[PASS] 有效尺寸计算测试通过\n")


def test_has_box_above():
    """测试上方箱子检测"""
    print("=" * 60)
    print("测试上方箱子检测")
    print("=" * 60)

    # 测试有上方箱子
    item = {
        'id': 1,
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100
    }
    above = {
        'id': 2,
        'position': {'x': 0, 'y': 0, 'z': 100},
        'length': 100,
        'width': 100,
        'height': 100
    }
    result = has_box_above(item, [item, above])
    print(f"箱子上方有其他箱子: {result}")
    assert result is True

    # 测试无上方箱子
    item = {
        'id': 1,
        'position': {'x': 0, 'y': 0, 'z': 100},
        'length': 100,
        'width': 100,
        'height': 100
    }
    below = {
        'id': 2,
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100
    }
    result = has_box_above(item, [item, below])
    print(f"箱子上方无其他箱子: {result}")
    assert result is False

    print("[PASS] 上方箱子检测测试通过\n")


def test_refresh_support_metrics():
    """测试支撑指标刷新"""
    print("=" * 60)
    print("测试支撑指标刷新")
    print("=" * 60)

    items = [
        {
            'position': {'x': 0, 'y': 0, 'z': 0},
            'length': 100,
            'width': 100,
            'height': 100
        },
        {
            'position': {'x': 0, 'y': 0, 'z': 100},
            'length': 100,
            'width': 100,
            'height': 100
        }
    ]

    refresh_support_metrics(items)

    print(f"地面箱子支撑比例: {items[0]['support_ratio']}")
    print(f"上层箱子支撑比例: {items[1]['support_ratio']}")
    assert items[0]['support_ratio'] == 1.0
    assert items[1]['support_ratio'] == 1.0
    assert 'supported_area' in items[0]
    assert 'supported_area' in items[1]

    print("[PASS] 支撑指标刷新测试通过\n")


def test_repack_ready_item():
    """测试重新装箱准备"""
    print("=" * 60)
    print("测试重新装箱准备")
    print("=" * 60)

    placed_item = {
        'id': 1,
        'raw_length': 100,
        'raw_width': 200,
        'raw_height': 300,
        'length': 102,
        'width': 202,
        'height': 300,
        'position': {'x': 0, 'y': 0, 'z': 0},
        'supported_area': 20000.0,
        'support_ratio': 1.0,
        'suction_box_corner': 'x_min_y_min',
        'suction_cup_corner': 'center',
        'suction_orientation': 0,
        'suction_cup_x_size': 600,
        'suction_cup_y_size': 800,
        'suction_rect_x_min': 0,
        'suction_rect_x_max': 600,
        'suction_rect_y_min': 0,
        'suction_rect_y_max': 800
    }

    repack_item = repack_ready_item(placed_item)

    print(f"原始箱子有position字段: {'position' in placed_item}")
    print(f"重新装箱箱子有position字段: {'position' in repack_item}")
    print(f"重新装箱箱子长度: {repack_item['length']}")
    print(f"重新装箱箱子宽度: {repack_item['width']}")
    print(f"重新装箱箱子高度: {repack_item['height']}")

    assert 'position' not in repack_item
    assert 'supported_area' not in repack_item
    assert 'support_ratio' not in repack_item
    assert 'suction_box_corner' not in repack_item
    assert repack_item['length'] == 100.0
    assert repack_item['width'] == 200.0
    assert repack_item['height'] == 300.0

    print("[PASS] 重新装箱准备测试通过\n")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("工具模块测试套件")
    print("=" * 60 + "\n")

    try:
        test_raw_dims()
        test_effective_dims()
        test_has_box_above()
        test_refresh_support_metrics()
        test_repack_ready_item()

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
