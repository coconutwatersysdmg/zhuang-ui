"""
救援模块测试

验证救援模块各组件的正确性。
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.rescue import PalletEvaluator, IndexBuilder, RescueOptimizer
import src.rescue.recipe_rebuilder as recipe_rebuilder


def test_pallet_evaluator():
    """测试托盘评估器"""
    print("=" * 60)
    print("测试托盘评估器")
    print("=" * 60)

    evaluator = PalletEvaluator()

    # 测试evaluate_pallet_solution
    packed = [
        {'min_pack_multiple': 10, 'length': 100, 'width': 100, 'height': 100}
    ]
    unfitted = []
    score, total_mpm, gap, met = evaluator.evaluate_pallet_solution(
        packed, unfitted, 20.0
    )
    print(f"评估结果: total_mpm={total_mpm}, gap={gap}, met={met}")
    assert total_mpm == 10
    assert gap == 10.0
    assert met is False

    # 测试calc_pallet_status
    solution = {
        'packed_items': [{'min_pack_multiple': 10}],
        'mpm_target': 20.0
    }
    status = evaluator.calc_pallet_status(solution)
    print(f"托盘状态: {status}")
    assert status == 'FAILED'
    assert solution['mpm_total'] == 10
    assert solution['mpm_gap'] == 10.0

    # 测试recompute_type_stats
    plans = [
        {'packed_items': [{'min_pack_multiple': 10}], 'mpm_target': 20.0},
        {'packed_items': [{'min_pack_multiple': 25}], 'mpm_target': 20.0}
    ]
    stats = evaluator.recompute_type_stats(plans)
    print(f"统计结果: {stats}")
    assert stats['total_pallets'] == 2
    assert stats['success_pallets'] == 1
    assert stats['failed_pallets'] == 1

    # 测试estimate_canonical_layer_best_mpm
    items = [
        {
            'type': 'A',
            'length': 100,
            'width': 100,
            'height': 100,
            'min_pack_multiple': 1.0
        }
    ] * 10
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    result = evaluator.estimate_canonical_layer_best_mpm(items, pallet_dims)
    print(f"典型层最佳MPM: {result['best_mpm']}")
    assert result['best_mpm'] >= 0

    print("[PASS] 托盘评估器测试通过\n")


def test_index_builder():
    """测试索引构建器"""
    print("=" * 60)
    print("测试索引构建器")
    print("=" * 60)

    builder = IndexBuilder()

    # 测试build_index_diagnostics
    items = [
        {'min_pack_multiple': 10.0},
        {'min_pack_multiple': 20.0}
    ]
    diag = builder.build_index_diagnostics(items, 25.0)
    print(f"诊断信息: total_mpm={diag['total_mpm']}, "
          f"theoretical_success={diag['theoretical_success_pallets']}")
    assert diag['total_mpm'] == 30.0
    assert diag['theoretical_success_pallets'] == 1

    # 测试build_index_bucket
    items = [
        {
            'id': 1,
            'min_pack_multiple': 10.0,
            'length': 100,
            'width': 100,
            'height': 100
        },
        {
            'id': 2,
            'min_pack_multiple': 15.0,
            'length': 100,
            'width': 100,
            'height': 100
        }
    ]
    bucket = builder.build_index_bucket(items, 20.0, seed=42)
    print(f"索引桶大小: {len(bucket)}")
    assert len(bucket) >= 0

    # 测试build_index_candidate_pool
    items = [
        {'id': i, 'min_pack_multiple': float(i)}
        for i in range(1, 11)
    ]
    pool = builder.build_index_candidate_pool(items, 20.0, seed=42)
    print(f"候选池大小: {len(pool)}")
    assert len(pool) > 0

    # 测试build_index_bucket_candidate_pool
    items = [
        {
            'id': i,
            'min_pack_multiple': float(i),
            'length': 100,
            'width': 100,
            'height': 100
        }
        for i in range(1, 11)
    ]
    pool = builder.build_index_bucket_candidate_pool(items, 20.0, seed=42)
    print(f"桶候选池大小: {len(pool)}")
    assert len(pool) > 0

    print("[PASS] 索引构建器测试通过\n")


def test_rescue_optimizer():
    """测试救援优化器"""
    print("=" * 60)
    print("测试救援优化器")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    optimizer = RescueOptimizer(pallet_dims=pallet_dims)

    # 测试optimize_failed_by_failed（空列表）
    result = optimizer.optimize_failed_by_failed([], 20.0)
    print(f"空列表优化结果: rescued={result['rescued']}")
    assert result['rescued'] == 0

    # 测试optimize_failed_by_failed（单个托盘）
    plans = [
        {
            'pallet_id': 1,
            'packed_items': [{'min_pack_multiple': 10}],
            'mpm_target': 20.0
        }
    ]
    result = optimizer.optimize_failed_by_failed(plans, 20.0)
    print(f"单托盘优化结果: rescued={result['rescued']}")
    assert 'rescued' in result

    # 测试_sum_mpm
    items = [
        {'min_pack_multiple': 10},
        {'min_pack_multiple': 20}
    ]
    total = optimizer._sum_mpm(items)
    print(f"MPM总和: {total}")
    assert total == 30

    # 无装箱器注入时，互借修复退化为只做诊断（与历史空实现行为一致）
    plans = [
        {
            'pallet_id': i,
            'packed_items': [{
                'id': f'b{i}',
                'min_pack_multiple': 10.0,
                'length': 100, 'width': 100, 'height': 100,
                'position': {'x': 0, 'y': 0, 'z': 0},
            }],
            'mpm_target': 20.0,
        }
        for i in range(2)
    ]
    result = optimizer.optimize_failed_by_failed(plans, 20.0)
    print(f"无装箱器诊断: reason={result['consolidate_reason']}")
    assert result['rescued'] == 0
    assert result['consolidate_reason'] == 'no_packer_injected'

    print("[PASS] 救援优化器测试通过\n")


def test_recipe_rebuild_does_not_create_empty_pallets():
    print("=" * 60)
    print("测试配方重建不生成空托盘")
    print("=" * 60)

    def _box(bid):
        return {
            'id': bid,
            'type': 'A',
            'length': 100,
            'width': 100,
            'height': 100,
            'weight': 1.0,
            'min_pack_multiple': 20.0,
            'position': {'x': 0, 'y': 0, 'z': 0},
            'pallet_dims': {'length': 1200, 'width': 1000, 'height': 1450},
            # 全量门禁必查字段（吸盘位姿），桩数据直接给占位值
            'suction_box_corner': 'BL',
            'suction_cup_corner': 'BL',
            'suction_orientation': 0,
            'suction_cup_x_size': 100,
            'suction_cup_y_size': 100,
            'suction_rect_x_min': 0,
            'suction_rect_x_max': 100,
            'suction_rect_y_min': 0,
            'suction_rect_y_max': 100,
        }

    class StubPacker:
        def __init__(self, *args, **kwargs):
            pass

        def pack(self, items, **kwargs):
            # 居中成排摆放，保证重心稳定且相邻贴紧
            packed = []
            n = len(items)
            start_x = (1200 - n * 100) / 2.0
            y = (1000 - 100) / 2.0
            for idx, item in enumerate(items):
                box = dict(item)
                box['position'] = {'x': start_x + idx * 100, 'y': y, 'z': 0}
                packed.append(box)
            return packed, []

    plans = [
        {
            'pallet_id': 'P1',
            'pallet_type': 'MH423C',
            'sales_order_no': 'O1',
            'packed_items': [_box(1), _box(2)],
            'mpm_target': 40.0,
        },
        {
            'pallet_id': 'P2',
            'pallet_type': 'MH423C',
            'sales_order_no': 'O1',
            'packed_items': [_box(3)],
            'mpm_target': 40.0,
        },
        {
            'pallet_id': 'P3',
            'pallet_type': 'MH423C',
            'sales_order_no': 'O1',
            'packed_items': [_box(4)],
            'mpm_target': 40.0,
        },
    ]
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    original_packer = recipe_rebuilder.BeamSearchPacker
    recipe_rebuilder.BeamSearchPacker = StubPacker
    try:
        diag = recipe_rebuilder.rescue_by_recipe_rebuild(
            plans, pallet_dims, target_mpm=40.0
        )
    finally:
        recipe_rebuilder.BeamSearchPacker = original_packer
    assert diag['recipe_rebuild_success'] == 1, diag
    assert all(p.get('packed_items') for p in plans)
    assert len(plans) == 2
    print(f"[PASS] recipe_rebuild_no_empty: pallets={len(plans)}\n")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("救援模块测试套件")
    print("=" * 60 + "\n")

    try:
        test_pallet_evaluator()
        test_index_builder()
        test_rescue_optimizer()
        test_recipe_rebuild_does_not_create_empty_pallets()

        print("=" * 60)
        print("[PASS] 所有测试通过！")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] 测试出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
