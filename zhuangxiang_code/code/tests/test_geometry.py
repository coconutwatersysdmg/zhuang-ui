"""
几何模块测试

验证几何计算函数的正确性。
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.geometry import (
    axis_overlap_len,
    has_positive_xy_overlap,
    calculate_direct_supported_area,
    direct_support_ratio,
    passes_box_gap_constraint,
    side_gap_flags,
    boundary_side_flags,
    validate_center_of_mass,
)


def test_axis_overlap_len():
    """测试轴向重叠长度计算"""
    print("=" * 60)
    print("测试轴向重叠长度计算")
    print("=" * 60)

    # 测试有重叠
    overlap = axis_overlap_len(0, 10, 5, 15)
    print(f"区间[0,10]与[5,15]的重叠长度: {overlap}")
    assert overlap == 5.0

    # 测试无重叠
    overlap = axis_overlap_len(0, 5, 10, 15)
    print(f"区间[0,5]与[10,15]的重叠长度: {overlap}")
    assert overlap == 0.0

    # 测试完全包含
    overlap = axis_overlap_len(0, 20, 5, 15)
    print(f"区间[0,20]与[5,15]的重叠长度: {overlap}")
    assert overlap == 10.0

    print("[PASS] 轴向重叠长度计算测试通过\n")


def test_has_positive_xy_overlap():
    """测试XY平面重叠检测"""
    print("=" * 60)
    print("测试XY平面重叠检测")
    print("=" * 60)

    point = {'x': 0, 'y': 0, 'z': 0}
    dims = {'length': 100, 'width': 100, 'height': 100}

    # 测试有重叠
    placed = {
        'position': {'x': 50, 'y': 50, 'z': 0},
        'length': 100,
        'width': 100
    }
    result = has_positive_xy_overlap(point, dims, placed)
    print(f"候选箱[0,0]与已放置箱[50,50]是否重叠: {result}")
    assert result is True

    # 测试无重叠
    placed = {
        'position': {'x': 200, 'y': 200, 'z': 0},
        'length': 100,
        'width': 100
    }
    result = has_positive_xy_overlap(point, dims, placed)
    print(f"候选箱[0,0]与已放置箱[200,200]是否重叠: {result}")
    assert result is False

    print("[PASS] XY平面重叠检测测试通过\n")


def test_calculate_direct_supported_area():
    """测试支撑面积计算"""
    print("=" * 60)
    print("测试支撑面积计算")
    print("=" * 60)

    # 测试地面上的箱子
    point = {'x': 0, 'y': 0, 'z': 0}
    dims = {'length': 100, 'width': 100, 'height': 100}
    placed = []
    area = calculate_direct_supported_area(point, dims, placed)
    print(f"地面上箱子的支撑面积: {area}")
    assert area == 10000.0

    # 测试完全支撑
    point = {'x': 0, 'y': 0, 'z': 100}
    dims = {'length': 100, 'width': 100, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100
    }]
    area = calculate_direct_supported_area(point, dims, placed)
    print(f"完全支撑的箱子支撑面积: {area}")
    assert area == 10000.0

    # 测试部分支撑
    point = {'x': 0, 'y': 0, 'z': 100}
    dims = {'length': 100, 'width': 100, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 50,
        'width': 50,
        'height': 100
    }]
    area = calculate_direct_supported_area(point, dims, placed)
    print(f"部分支撑的箱子支撑面积: {area}")
    assert area == 2500.0

    print("[PASS] 支撑面积计算测试通过\n")


def test_direct_support_ratio():
    """测试支撑比例计算"""
    print("=" * 60)
    print("测试支撑比例计算")
    print("=" * 60)

    # 测试完全支撑
    point = {'x': 0, 'y': 0, 'z': 100}
    dims = {'length': 100, 'width': 100, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100
    }]
    ratio = direct_support_ratio(point, dims, placed)
    print(f"完全支撑的箱子支撑比例: {ratio}")
    assert ratio == 1.0

    # 测试部分支撑
    point = {'x': 0, 'y': 0, 'z': 100}
    dims = {'length': 100, 'width': 100, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 50,
        'width': 50,
        'height': 100
    }]
    ratio = direct_support_ratio(point, dims, placed)
    print(f"部分支撑的箱子支撑比例: {ratio}")
    assert ratio == 0.25

    print("[PASS] 支撑比例计算测试通过\n")


def test_passes_box_gap_constraint():
    """测试箱间间隙约束"""
    print("=" * 60)
    print("测试箱间间隙约束")
    print("=" * 60)

    # 测试满足间隙约束
    point = {'x': 100, 'y': 0, 'z': 0}
    dims = {'length': 100, 'width': 100, 'height': 100}
    raw_dims = {'length': 98, 'width': 98, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100,
        'raw_length': 98,
        'raw_width': 98,
        'raw_height': 100
    }]
    result = passes_box_gap_constraint(point, dims, raw_dims, placed, max_gap=6.0)
    print(f"间隙2mm，是否满足约束(max_gap=6mm): {result}")
    assert result is True

    # 测试不满足间隙约束
    point = {'x': 100, 'y': 0, 'z': 0}
    dims = {'length': 100, 'width': 100, 'height': 100}
    raw_dims = {'length': 90, 'width': 90, 'height': 100}
    placed = [{
        'position': {'x': 0, 'y': 0, 'z': 0},
        'length': 100,
        'width': 100,
        'height': 100,
        'raw_length': 90,
        'raw_width': 90,
        'raw_height': 100
    }]
    result = passes_box_gap_constraint(point, dims, raw_dims, placed, max_gap=6.0)
    print(f"间隙10mm，是否满足约束(max_gap=6mm): {result}")
    assert result is False

    # 锚定语义：贴紧一侧邻箱后，对面残余大间隙不违规（行尾残缝）
    point = {'x': 400, 'y': 0, 'z': 0}
    dims = {'length': 400, 'width': 300, 'height': 200}
    placed = [
        {
            'position': {'x': 0, 'y': 0, 'z': 0},
            'length': 400, 'width': 300, 'height': 200,
            'raw_length': 400, 'raw_width': 300, 'raw_height': 200,
        },
        {
            'position': {'x': 830, 'y': 0, 'z': 0},
            'length': 400, 'width': 300, 'height': 200,
            'raw_length': 400, 'raw_width': 300, 'raw_height': 200,
        },
    ]
    result = passes_box_gap_constraint(point, dims, dims, placed, max_gap=6.0)
    print(f"贴紧左邻、右余30mm（行尾残缝）: {result}")
    assert result is True

    # 锚定语义：两侧均不贴紧（浮空）仍拒绝
    point = {'x': 415, 'y': 0, 'z': 0}
    dims = {'length': 250, 'width': 300, 'height': 200}
    placed = [
        {
            'position': {'x': 0, 'y': 0, 'z': 0},
            'length': 400, 'width': 300, 'height': 200,
            'raw_length': 400, 'raw_width': 300, 'raw_height': 200,
        },
        {
            'position': {'x': 680, 'y': 0, 'z': 0},
            'length': 400, 'width': 300, 'height': 200,
            'raw_length': 400, 'raw_width': 300, 'raw_height': 200,
        },
    ]
    result = passes_box_gap_constraint(point, dims, dims, placed, max_gap=6.0)
    print(f"两侧各15mm浮空: {result}")
    assert result is False

    # 锚定语义：浮空但推到托盘边（提供 pallet_dims 时靠边算锚定）
    point = {'x': 950, 'y': 0, 'z': 0}
    dims = {'length': 250, 'width': 300, 'height': 200}
    placed = [
        {
            'position': {'x': 0, 'y': 0, 'z': 0},
            'length': 400, 'width': 300, 'height': 200,
            'raw_length': 400, 'raw_width': 300, 'raw_height': 200,
        },
    ]
    result = passes_box_gap_constraint(
        point, dims, dims, placed, max_gap=6.0,
        pallet_dims={'length': 1200, 'width': 1000},
    )
    print(f"贴托盘右边、左距邻箱550mm: {result}")
    assert result is True
    result = passes_box_gap_constraint(point, dims, dims, placed, max_gap=6.0)
    print(f"同摆放不提供pallet_dims: {result}")
    assert result is False

    print("[PASS] 箱间间隙约束测试通过\n")


def test_validate_center_of_mass():
    """测试重心验证"""
    print("=" * 60)
    print("测试重心验证")
    print("=" * 60)

    # 测试稳定的重心
    pallet_plan = {
        'packed_items': [
            {
                'position': {'x': 550, 'y': 450, 'z': 0},
                'length': 100,
                'width': 100,
                'height': 100,
                'weight': 10.0
            }
        ]
    }
    pallet_dims = {'length': 1200, 'width': 1000}
    result = validate_center_of_mass(pallet_plan, pallet_dims)
    print(f"重心位置: ({result['center_of_mass']['x']:.1f}, {result['center_of_mass']['y']:.1f})")
    print(f"托盘中心: ({result['pallet_center']['x']:.1f}, {result['pallet_center']['y']:.1f})")
    print(f"是否稳定: {result['is_stable']}")
    assert result['is_stable'] is True

    # 测试不稳定的重心
    pallet_plan = {
        'packed_items': [
            {
                'position': {'x': 0, 'y': 0, 'z': 0},
                'length': 100,
                'width': 100,
                'height': 100,
                'weight': 10.0
            }
        ]
    }
    pallet_dims = {'length': 1200, 'width': 1000}
    result = validate_center_of_mass(pallet_plan, pallet_dims)
    print(f"\n重心位置: ({result['center_of_mass']['x']:.1f}, {result['center_of_mass']['y']:.1f})")
    print(f"托盘中心: ({result['pallet_center']['x']:.1f}, {result['pallet_center']['y']:.1f})")
    print(f"是否稳定: {result['is_stable']}")
    if not result['is_stable']:
        print(f"X方向偏移: {result['offset_x_percent']:.1f}%")
        print(f"Y方向偏移: {result['offset_y_percent']:.1f}%")
    assert result['is_stable'] is False

    print("[PASS] 重心验证测试通过\n")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("几何模块测试套件")
    print("=" * 60 + "\n")

    try:
        test_axis_overlap_len()
        test_has_positive_xy_overlap()
        test_calculate_direct_supported_area()
        test_direct_support_ratio()
        test_passes_box_gap_constraint()
        test_validate_center_of_mass()

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
