"""
FailedPoolRebuilder 单元测试

注入 stub 装箱原语，验证：
1. 池总 mpm 不足时跳过；
2. 池足够时能装出新的 SUCCESS 盘并替换；
3. 几何连续失败触发预算终止；
4. 守恒：救援前后箱 id 集合一致。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rescue import FailedPoolRebuilder, LowLoadRebuilder, TailFragmentAbsorber


def _make_failed_plan(pid, items, target=192.0):
    """构造一个 FAILED 托盘方案。"""
    total = sum(b['min_pack_multiple'] for b in items)
    return {
        'pallet_id': f'MH423C-O1-{pid}',
        'pallet_type': 'MH423C',
        'sales_order_no': 'O1',
        'packed_items': items,
        'mpm_total': total,
        'mpm_target': target,
        'mpm_gap': target - total,
        'mpm_status': 'FAILED',
    }


def _make_box(bid, mpm, l=200, w=200, h=200):
    return {
        'id': bid,
        'min_pack_multiple': mpm,
        'length': l,
        'width': w,
        'height': h,
        'raw_length': l,
        'raw_width': w,
        'raw_height': h,
        'position': {'x': 0, 'y': 0, 'z': 0},
        'support_ratio': 1.0,
        'supported_area': l * w,
    }


def _placed_box(bid, mpm, x, y, l=200, w=200, h=200):
    box = _make_box(bid, mpm, l=l, w=w, h=h)
    box['position'] = {'x': x, 'y': y, 'z': 0}
    box['suction_box_corner'] = 'x_min_y_min'
    box['suction_cup_corner'] = 'x_min_y_min'
    box['suction_orientation'] = 'cup_600x_800y'
    box['suction_cup_x_size'] = 600.0
    box['suction_cup_y_size'] = 800.0
    box['suction_rect_x_min'] = x
    box['suction_rect_x_max'] = x + 600.0
    box['suction_rect_y_min'] = y
    box['suction_rect_y_max'] = y + 800.0
    return box


class _GreedyStubPacker:
    """模拟 CustomPacker：贪心放入直到达到 target 或没箱子可放。"""

    def __init__(self, pallet_dims, **kwargs):
        self.pallet_dims = pallet_dims

    def pack(self, items, target_mpm=None, **kwargs):
        packed = []
        total = 0.0
        x = 200.0
        y = 400.0
        row_w = 0.0
        for item in items:
            new_item = dict(item)
            length = float(new_item.get('length', 0) or 0)
            width = float(new_item.get('width', 0) or 0)
            if x + length > self.pallet_dims.get('length', 1200):
                x = 200.0
                y += row_w
                row_w = 0.0
            new_item['position'] = {'x': x, 'y': y, 'z': 0.0}
            new_item['suction_box_corner'] = 'x_min_y_min'
            new_item['suction_cup_corner'] = 'x_min_y_min'
            new_item['suction_orientation'] = 'cup_600x_800y'
            new_item['suction_cup_x_size'] = 600.0
            new_item['suction_cup_y_size'] = 800.0
            new_item['suction_rect_x_min'] = x
            new_item['suction_rect_x_max'] = x + 600.0
            new_item['suction_rect_y_min'] = y
            new_item['suction_rect_y_max'] = y + 800.0
            x += length
            row_w = max(row_w, width)
            packed.append(new_item)
            total += item.get('min_pack_multiple', 0)
            if target_mpm is not None and total >= target_mpm:
                break
        return packed, []


def test_skip_when_pool_below_target():
    rebuilder = FailedPoolRebuilder(
        custom_packer_cls=_GreedyStubPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )
    plans = [
        _make_failed_plan(1, [_make_box(1, 50)]),
        _make_failed_plan(2, [_make_box(2, 60)]),
    ]
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = rebuilder.rebuild(plans, pallet_dims, target_mpm=192.0)
    assert diag['skipped'] is True
    assert diag['reason'] == 'pool_mpm_below_target'
    assert diag['rescued'] == 0
    print(f"[PASS] skip_when_pool_below_target: {diag['reason']}")


def test_rebuild_success():
    rebuilder = FailedPoolRebuilder(
        custom_packer_cls=_GreedyStubPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )
    # 4 个失败盘 × 100 mpm = 400 总池，target=192，应至少装出 2 个 SUCCESS
    plans = [
        _make_failed_plan(i, [_make_box(i * 10 + j, 50) for j in range(2)])
        for i in range(1, 5)
    ]
    original_ids = set()
    for p in plans:
        for b in p['packed_items']:
            original_ids.add(b['id'])

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = rebuilder.rebuild(plans, pallet_dims, target_mpm=192.0)

    assert diag['skipped'] is False, diag
    assert diag['rescued'] >= 1, diag
    assert diag['box_conservation_ok'] is True

    # 守恒：救援后总 id 应与原集合一致
    new_ids = set()
    for p in plans:
        for b in p['packed_items']:
            new_ids.add(b['id'])
    assert new_ids == original_ids, (
        f"id 不守恒: 缺 {original_ids - new_ids}，多 {new_ids - original_ids}"
    )

    success_count = sum(
        1 for p in plans if p.get('mpm_status') == 'SUCCESS'
    )
    assert success_count == diag['rescued']
    print(
        f"[PASS] rebuild_success: rescued={diag['rescued']}, "
        f"attempts={diag['rebuild_attempts']}"
    )


def test_geometry_fail_streak_terminates():
    class AlwaysFailPacker:
        def __init__(self, *a, **k):
            pass

        def pack(self, items, **kwargs):
            return [], []

    rebuilder = FailedPoolRebuilder(
        custom_packer_cls=AlwaysFailPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
        max_geometry_fail_streak=2,
    )
    plans = [
        _make_failed_plan(i, [_make_box(i * 10 + j, 50) for j in range(2)])
        for i in range(1, 5)
    ]
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = rebuilder.rebuild(plans, pallet_dims, target_mpm=192.0)
    assert diag['rescued'] == 0
    assert diag['reason'] in (
        'geometry_fail_streak', 'no_success_built',
        'packed_mpm_below_target',
    )
    assert diag['rebuild_attempts'] <= 2 + 1
    print(
        f"[PASS] geometry_fail_streak_terminates: reason={diag['reason']}, "
        f"attempts={diag['rebuild_attempts']}"
    )


def test_no_target():
    rebuilder = FailedPoolRebuilder(
        custom_packer_cls=_GreedyStubPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )
    diag = rebuilder.rebuild([], {}, target_mpm=None)
    assert diag['skipped'] is True
    assert diag['reason'] == 'no_target'
    print("[PASS] no_target")


def test_low_load_rebuilder_compacts_failed_tails():
    rebuilder = LowLoadRebuilder(
        custom_packer_cls=_GreedyStubPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
        low_box_count=2,
        low_mpm=32.0,
        deep_gap=160.0,
    )
    plans = [
        _make_failed_plan(i, [_make_box(i * 10 + j, 50) for j in range(2)])
        for i in range(1, 5)
    ]
    original_ids = {
        b['id'] for p in plans for b in p['packed_items']
    }
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = rebuilder.rebuild(plans, pallet_dims, target_mpm=192.0)
    new_ids = {b['id'] for p in plans for b in p['packed_items']}

    assert diag['low_load_tried'] == 1, diag
    assert diag['low_load_accepted'] == 1, diag
    assert new_ids == original_ids
    assert sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS') >= 1
    print(
        f"[PASS] low_load_rebuilder_compacts_failed_tails: "
        f"{diag['low_load_old_pallets']}->{diag['low_load_new_pallets']}"
    )


def test_low_load_rebuilder_rejects_more_failed_pallets():
    rebuilder = LowLoadRebuilder(
        custom_packer_cls=_GreedyStubPacker,
        build_direct_layer_solution=lambda items, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
        low_box_count=6,
        low_mpm=32.0,
        deep_gap=160.0,
    )
    plans = [
        _make_failed_plan(
            i,
            [_make_box(i * 10 + j, 10, l=1000, w=1000, h=100) for j in range(5)],
        )
        for i in range(1, 3)
    ]
    original_count = len(plans)
    original_ids = {
        b['id'] for p in plans for b in p['packed_items']
    }
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = rebuilder.rebuild(plans, pallet_dims, target_mpm=192.0)
    new_ids = {b['id'] for p in plans for b in p['packed_items']}

    assert diag['low_load_tried'] == 1, diag
    assert diag['low_load_accepted'] == 0, diag
    assert len(plans) == original_count
    assert new_ids == original_ids
    print("[PASS] low_load_rebuilder_rejects_more_failed_pallets")


def test_tail_fragment_absorber_moves_low_tail_into_success_pallet():
    absorber = TailFragmentAbsorber(
        low_box_count=2,
        low_mpm=32.0,
        deep_gap=160.0,
        max_attempts=10,
    )
    plans = [
        {
            'pallet_id': 'S1',
            'pallet_type': 'MH423C',
            'sales_order_no': 'O1',
            'packed_items': [
                _placed_box(1, 200, 500, 400, l=200, w=200),
            ],
            'mpm_target': 192.0,
        },
        _make_failed_plan(
            2,
            [_placed_box(2, 5, 0, 0, l=100, w=100)],
            target=192.0,
        ),
    ]
    original_ids = {
        b['id'] for p in plans for b in p['packed_items']
    }
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    diag = absorber.absorb(plans, pallet_dims, target_mpm=192.0)
    new_ids = {b['id'] for p in plans for b in p['packed_items']}

    assert diag['tail_absorb_success'] == 1, diag
    assert diag['tail_absorb_donor_emptied'] == 1, diag
    assert sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS') == 1
    assert new_ids == original_ids
    assert len(plans[0]['packed_items']) == 2
    assert plans[1]['packed_items'] == []
    print("[PASS] tail_fragment_absorber_moves_low_tail_into_success_pallet")


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("FailedPoolRebuilder 测试套件")
    print("=" * 60 + "\n")
    try:
        test_no_target()
        test_skip_when_pool_below_target()
        test_geometry_fail_streak_terminates()
        test_rebuild_success()
        test_low_load_rebuilder_compacts_failed_tails()
        test_low_load_rebuilder_rejects_more_failed_pallets()
        test_tail_fragment_absorber_moves_low_tail_into_success_pallet()
        print("\n" + "=" * 60)
        print("[PASS] 所有测试通过！")
        print("=" * 60)
    except AssertionError as e:
        import traceback
        print(f"\n[FAIL] {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)
