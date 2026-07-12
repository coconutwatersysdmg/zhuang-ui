"""
主流程模块测试

验证 OrderProcessor、PalletPacker、ResultFormatter、PackingWorkflow
的接口与基本行为。装箱原语通过 stub 注入，避免依赖真实 Excel 数据。
"""

import sys
import tempfile
from pathlib import Path

import pandas as pd

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.main import (
    OrderProcessor,
    PalletPacker,
    ResultFormatter,
    PackingWorkflow,
    build_json_output_plan,
)
from src.main.report_persister import JsonFileReportPersister
from src.geometry.constraint_validator import validate_pallet_constraints
from src.geometry.center_of_mass import validate_center_of_mass
from src.packing.beam_search_packer import BeamSearchPacker
from src.packing.direct_layer_packer import (
    build_centered_single_box_solution,
    build_direct_layer_packing_solution,
)
from src.rescue import IndexBuilder, PalletEvaluator


def test_order_processor():
    print("=" * 60)
    print("测试 OrderProcessor")
    print("=" * 60)

    sample = [
        {'id': 1, 'pallet_type': 'A', 'sales_order_no': 'O1'},
        {'id': 2, 'pallet_type': 'A', 'sales_order_no': 'O1'},
        {'id': 3, 'pallet_type': 'B', 'sales_order_no': 'O2'},
        {'id': 4, 'pallet_type': 'A'},  # 缺销售订单号
    ]

    op = OrderProcessor(preprocess_fn=lambda *a, **k: sample)
    boxes, grouped = op.prepare()
    assert len(boxes) == 4
    assert len(grouped[('A', 'O1')]) == 2
    assert ('A', 'UNKNOWN_ORDER') in grouped
    print(f"分组结果: {sorted(grouped.keys())}")
    print("[PASS] OrderProcessor\n")


def _make_stub_packer():
    """构造模拟 CustomPacker，包装一个箱子即返回。"""

    class StubPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def pack(self, pool, **kwargs):
            return list(pool)[:2], {}

    return StubPacker


def test_report_persister_writes_pallet_excel_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        persister = JsonFileReportPersister(
            Path(tmpdir),
            lambda fmt: "20260522_120000",
        )
        report = {
            "pallets": [{
                "pallet_id": "P1",
                "packed_items": [{
                    "id": "B1",
                    "pallet_dims": {"length": 1440, "width": 2240, "height": 720},
                }],
                "stability_checks": {"status": "SUCCESS"},
                "mpm_total": 192.0,
                "mpm_target": 192.0,
                "mpm_gap": 0.0,
                "mpm_status": "SUCCESS",
            }]
        }

        persister.persist(report, 1.23)

        excel_path = Path(tmpdir) / "packing_plan_summary_20260522_120000.xlsx"
        assert excel_path.exists()
        df = pd.read_excel(excel_path)
        assert list(df.columns) == [
            "托盘ID",
            "托盘尺寸(mm)",
            "箱子数量",
            "稳定性状态",
            "指数",
            "目标指数",
            "指数缺口",
            "指数状态",
        ]
        assert df.iloc[0]["托盘ID"] == "P1"
        assert df.iloc[0]["托盘尺寸(mm)"] == "1440x2240x720"
        assert int(df.iloc[0]["箱子数量"]) == 1


def test_candidate_selection_preserves_success_potential():
    packer = PalletPacker(
        custom_packer_cls=_make_stub_packer(),
        build_direct_layer_solution=lambda *a, **k: [],
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=lambda solution, dims: {"is_stable": True},
    )
    source = [
        {"id": "A", "min_pack_multiple": 100, "length": 10, "width": 10, "height": 10},
        {"id": "B", "min_pack_multiple": 120, "length": 100, "width": 100, "height": 10},
        {"id": "C", "min_pack_multiple": 30, "length": 100, "width": 100, "height": 10},
    ]
    lower_fill_preserves_future = [source[0]]
    higher_fill_loses_future = [source[0], source[1]]

    selected = packer._select_best_packed_candidate(
        [higher_fill_loses_future, lower_fill_preserves_future],
        source,
        target_mpm=100,
    )

    assert [item["id"] for item in selected] == ["A"]


def test_pallet_packer():
    print("=" * 60)
    print("测试 PalletPacker")
    print("=" * 60)

    def _direct(items, **kwargs):
        return []  # 强制走 CustomPacker 分支

    def _centered(items, *a, **k):
        return []

    def _com(solution, dims):
        return {"is_stable": True}

    packer = PalletPacker(
        custom_packer_cls=_make_stub_packer(),
        build_direct_layer_solution=_direct,
        build_centered_single_box_solution=_centered,
        validate_center_of_mass=_com,
    )

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'pallet_type': 'A',
            'sales_order_no': 'O1',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 15.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(1, 5)
    ]
    type_plan, runtime, diag = packer.pack_group('A', 'O1', boxes, target_mpm=20.0)
    print(f"生成托盘数: {len(type_plan)}")
    print(f"耗时: {runtime}")
    assert len(type_plan) >= 1
    assert 'packing' in runtime
    assert 'total_mpm' in diag
    print("[PASS] PalletPacker\n")


def test_pallet_packer_conservation_fallback():
    print("=" * 60)
    print("测试 PalletPacker 守恒兜底")
    print("=" * 60)

    class EmptyPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def pack(self, pool, **kwargs):
            return [], {}

    def _direct(items, **kwargs):
        return []

    def _centered(items, pallet_dims, **kwargs):
        if len(items) != 1:
            return []
        item = dict(items[0])
        item['position'] = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        item['length'] = item['length'] + kwargs.get('xy_tolerance', 0.0)
        item['width'] = item['width'] + kwargs.get('xy_tolerance', 0.0)
        item['height'] = item['height'] + kwargs.get('z_tolerance', 0.0)
        return [item]

    packer = PalletPacker(
        custom_packer_cls=EmptyPacker,
        build_direct_layer_solution=_direct,
        build_centered_single_box_solution=_centered,
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'pallet_type': 'A',
            'sales_order_no': 'O1',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 1.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(1, 4)
    ]
    type_plan, _, _ = packer.pack_group('A', 'O1', boxes, target_mpm=20.0)
    packed_ids = [
        item['id']
        for pallet in type_plan
        for item in pallet.get('packed_items', [])
    ]
    assert sorted(packed_ids) == [1, 2, 3]
    assert len(packed_ids) == len(set(packed_ids))
    assert all(p.get('conservation_fallback') for p in type_plan)
    print(f"守恒兜底托盘数: {len(type_plan)}")
    print("[PASS] PalletPacker 守恒兜底\n")


def test_main_score_prefers_fuller_target_met_pallet():
    print("=" * 60)
    print("测试主装箱评分：达标后优先更满")
    print("=" * 60)

    small = [{'min_pack_multiple': 20, 'length': 100, 'width': 100, 'height': 100}]
    fuller = [
        {'min_pack_multiple': 12, 'length': 100, 'width': 100, 'height': 100},
        {'min_pack_multiple': 12, 'length': 100, 'width': 100, 'height': 100},
    ]
    score_small, *_ = PalletEvaluator.evaluate_pallet_solution(
        small, [{'min_pack_multiple': 1}], 20.0
    )
    score_fuller, *_ = PalletEvaluator.evaluate_pallet_solution(
        fuller, [], 20.0
    )
    assert score_fuller > score_small
    print("[PASS] 主装箱评分\n")


def test_main_score_prefers_closer_gap_before_future_tail_when_not_met():
    print("=" * 60)
    print("测试主装箱评分：未达标时优先缩小当前缺口")
    print("=" * 60)

    weak_current = [
        {'min_pack_multiple': 40, 'length': 100, 'width': 100, 'height': 100}
    ]
    stronger_current = [
        {'min_pack_multiple': 180, 'length': 100, 'width': 100, 'height': 100}
    ]
    large_remaining = [
        {'min_pack_multiple': 192, 'length': 100, 'width': 100, 'height': 100}
    ]
    small_remaining = [
        {'min_pack_multiple': 52, 'length': 100, 'width': 100, 'height': 100}
    ]

    weak_score, *_ = PalletEvaluator.evaluate_pallet_solution(
        weak_current, large_remaining, 192.0
    )
    strong_score, *_ = PalletEvaluator.evaluate_pallet_solution(
        stronger_current, small_remaining, 192.0
    )
    assert strong_score > weak_score
    print("[PASS] 未达标缺口优先\n")


def test_main_score_prefers_fuller_target_met_over_exact_but_sparse():
    print("=" * 60)
    print("测试主装箱评分：达标后优先满盘而非刚好达标")
    print("=" * 60)

    exact_sparse = [
        {'min_pack_multiple': 192, 'length': 100, 'width': 100, 'height': 100}
    ]
    fuller_over = [
        {'min_pack_multiple': 112, 'length': 300, 'width': 300, 'height': 300},
        {'min_pack_multiple': 112, 'length': 300, 'width': 300, 'height': 300},
    ]
    same_future_remaining = [
        {'min_pack_multiple': 384, 'length': 100, 'width': 100, 'height': 100}
    ]

    exact_score, *_ = PalletEvaluator.evaluate_pallet_solution(
        exact_sparse, same_future_remaining, 192.0
    )
    fuller_score, *_ = PalletEvaluator.evaluate_pallet_solution(
        fuller_over, same_future_remaining, 192.0
    )
    assert fuller_score > exact_score
    print("[PASS] 达标满盘优先\n")


def test_pallet_candidate_pool_includes_fill_items_after_index_target():
    print("=" * 60)
    print("测试主候选池：指数候选后继续加入填充候选")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': 'idx1',
            'type': 'IDX',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 10.0,
            'length': 100,
            'width': 100,
            'height': 100,
        },
        {
            'id': 'idx2',
            'type': 'IDX',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 10.0,
            'length': 100,
            'width': 100,
            'height': 100,
        },
    ]
    boxes.extend(
        {
            'id': f'fill{i}',
            'type': 'FILL',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 1.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(6)
    )

    pool = IndexBuilder.build_pallet_candidate_pool(
        boxes, target_mpm=20.0, seed=1, pallet_dims=pallet_dims
    )
    ids = {box['id'] for box in pool}
    assert {'idx1', 'idx2'}.issubset(ids)
    assert any(str(box_id).startswith('fill') for box_id in ids)
    assert len(pool) > 2
    print("[PASS] 主候选池\n")


def test_target_pack_does_not_stop_when_target_met_by_default():
    print("=" * 60)
    print("测试主装箱：普通目标不再达标即停")
    print("=" * 60)

    calls = []

    class CapturingPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def pack(self, pool, **kwargs):
            calls.append(kwargs)
            return [], {}

    packer = PalletPacker(
        custom_packer_cls=CapturingPacker,
        build_direct_layer_solution=lambda *a, **k: [],
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'type': 'A',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 10.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(4)
    ]
    packer._initial_pack(
        boxes, target_mpm=20.0, pallet_dims=pallet_dims, pallet_counter=1
    )
    assert calls
    assert calls[0]['stop_when_target_met'] is False
    print("[PASS] 主装箱不达标即停\n")


def test_initial_pack_compares_direct_and_fill_candidates():
    print("=" * 60)
    print("测试主装箱：每盘比较多个候选")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'type': 'A',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 12.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(1, 4)
    ]

    def _placed(item, x):
        box = dict(item)
        box['position'] = {'x': x, 'y': 449.0, 'z': 0.0}
        box['raw_length'] = box['length']
        box['raw_width'] = box['width']
        box['raw_height'] = box['height']
        box['length'] = box['length'] + 2.0
        box['width'] = box['width'] + 2.0
        box['suction_box_corner'] = 'x_min_y_min'
        box['suction_cup_corner'] = 'x_min_y_min'
        box['suction_orientation'] = 'cup_600x_800y'
        box['suction_cup_x_size'] = 600.0
        box['suction_cup_y_size'] = 800.0
        box['suction_rect_x_min'] = box['position']['x']
        box['suction_rect_x_max'] = box['position']['x'] + 600.0
        box['suction_rect_y_min'] = box['position']['y']
        box['suction_rect_y_max'] = box['position']['y'] + 800.0
        return box

    def _direct(items, **kwargs):
        return [_placed(items[0], 0.0), _placed(items[1], 102.0)]

    class FullerPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def pack(self, pool, **kwargs):
            return [
                _placed(pool[0], 0.0),
                _placed(pool[1], 102.0),
                _placed(pool[2], 204.0),
            ], {}

    packer = PalletPacker(
        custom_packer_cls=FullerPacker,
        build_direct_layer_solution=_direct,
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
    )
    packed = packer._initial_pack(
        boxes, target_mpm=20.0, pallet_dims=pallet_dims, pallet_counter=1
    )
    assert len(packed) == 3
    print("[PASS] 多候选比较\n")


def test_main_flow_only_fills_when_future_success_is_preserved():
    print("=" * 60)
    print("测试主流程：补箱不牺牲后续达标潜力")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'type': 'A',
            'pallet_type': 'MH110',
            'sales_order_no': 'O1',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 16.0 if i < 2 else 1.0,
            'length': 100,
            'width': 100,
            'height': 100,
            'weight': 1.0,
        }
        for i in range(8)
    ]

    def _placed(item, x):
        box = dict(item)
        box['position'] = {'x': x, 'y': 449.0, 'z': 0.0}
        box['raw_length'] = box['length']
        box['raw_width'] = box['width']
        box['raw_height'] = box['height']
        box['length'] = box['length'] + 2.0
        box['width'] = box['width'] + 2.0
        box['height'] = box['height']
        box['suction_box_corner'] = 'x_min_y_min'
        box['suction_cup_corner'] = 'x_min_y_min'
        box['suction_orientation'] = 'cup_600x_800y'
        box['suction_cup_x_size'] = 600.0
        box['suction_cup_y_size'] = 800.0
        box['suction_rect_x_min'] = x
        box['suction_rect_x_max'] = x + 600.0
        box['suction_rect_y_min'] = 449.0
        box['suction_rect_y_max'] = 1249.0
        return box

    def _direct(items, **kwargs):
        if len(items) < 2:
            return []
        return [_placed(items[0], 192.0), _placed(items[1], 294.0)]

    class FastRowPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def pack(self, pool, **kwargs):
            if len(pool) < 2:
                return [], {}
            return [_placed(pool[0], 192.0), _placed(pool[1], 294.0)], {}

        def _generate_feasible_candidates(self, item, placed_boxes, rng):
            x = 192.0 + len(placed_boxes) * 102.0
            if x + 102.0 > self.pallet_dims['length']:
                return []
            return [{'box': _placed(item, x), 'score': (0.0, 0.0, x, -1.0)}]

    packer = PalletPacker(
        custom_packer_cls=FastRowPacker,
        build_direct_layer_solution=_direct,
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=validate_center_of_mass,
    )
    type_plan, _, _ = packer.pack_group(
        'MH110', 'O1', boxes, target_mpm=32.0
    )
    packed_ids = [
        item['id']
        for pallet in type_plan
        for item in pallet.get('packed_items', [])
    ]

    assert sorted(packed_ids) == list(range(8))
    assert len(packed_ids) == len(set(packed_ids))
    assert type_plan
    assert len(type_plan[0].get('packed_items', [])) > 2
    assert sum(
        1 for item in type_plan[0].get('packed_items', [])
        if float(item.get('min_pack_multiple') or 0) >= 16.0
    ) == 2
    assert all(
        validate_pallet_constraints(pallet, pallet_dims)["is_valid"]
        for pallet in type_plan
    )
    print(
        f"托盘数: {len(type_plan)}, "
        f"每盘箱数: {[len(p['packed_items']) for p in type_plan]}"
    )
    print("[PASS] 主流程补箱潜力保护\n")


def test_main_topup_rejects_fill_that_reduces_future_success():
    print("=" * 60)
    print("测试主流程：拒绝牺牲后续达标数的补箱")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'type': 'A',
            'pallet_type': 'MH110',
            'sales_order_no': 'O1',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 16.0,
            'length': 100,
            'width': 100,
            'height': 100,
            'weight': 1.0,
        }
        for i in range(8)
    ]

    def _placed(item, x):
        box = dict(item)
        box['position'] = {'x': x, 'y': 449.0, 'z': 0.0}
        box['raw_length'] = box['length']
        box['raw_width'] = box['width']
        box['raw_height'] = box['height']
        box['length'] = box['length'] + 2.0
        box['width'] = box['width'] + 2.0
        box['height'] = box['height']
        box['suction_box_corner'] = 'x_min_y_min'
        box['suction_cup_corner'] = 'x_min_y_min'
        box['suction_orientation'] = 'cup_600x_800y'
        box['suction_cup_x_size'] = 600.0
        box['suction_cup_y_size'] = 800.0
        box['suction_rect_x_min'] = x
        box['suction_rect_x_max'] = x + 600.0
        box['suction_rect_y_min'] = 449.0
        box['suction_rect_y_max'] = 1249.0
        return box

    class FastRowPacker:
        def __init__(self, pallet_dims, **kwargs):
            self.pallet_dims = pallet_dims

        def _generate_feasible_candidates(self, item, placed_boxes, rng):
            x = 192.0 + len(placed_boxes) * 102.0
            return [{'box': _placed(item, x), 'score': (0.0, 0.0, x, -1.0)}]

    packer = PalletPacker(
        custom_packer_cls=FastRowPacker,
        build_direct_layer_solution=lambda *a, **k: [],
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=validate_center_of_mass,
    )
    packed, remaining = packer._top_up_current_pallet_to_fill(
        [_placed(boxes[0], 192.0), _placed(boxes[1], 294.0)],
        boxes[2:],
        pallet_dims,
        target_mpm=32.0,
    )
    assert len(packed) == 2
    assert len(remaining) == 6
    print("[PASS] 拒绝牺牲后续达标数的补箱\n")


def test_result_formatter():
    print("=" * 60)
    print("测试 ResultFormatter")
    print("=" * 60)

    plans = [
        {
            'packed_items': [{'min_pack_multiple': 25}],
            'mpm_target': 20.0,
            'mpm_status': 'SUCCESS',
            'mpm_gap': -5.0,
        },
        {
            'packed_items': [{'min_pack_multiple': 12}],
            'mpm_target': 20.0,
            'mpm_status': 'FAILED',
            'mpm_gap': 8.0,
        },
    ]
    stats = ResultFormatter.build_type_stats(
        type_plan=plans,
        pallet_type='A',
        sales_order_no='O1',
        index_diag={'total_mpm': 37.0},
        rescued_cnt=0,
        runtime={'packing': 0.5, 'retry': 0.2, 'repack': 0.1, 'total': 0.9},
        repack_result={'pair_tried': 4, 'pair_improved': 1},
    )
    assert stats['total_pallets'] == 2
    assert stats['kpi']['pair_efficiency'] == 0.25
    print(f"分组统计 OK: total={stats['total_pallets']}, "
          f"kpi.pair_efficiency={stats['kpi']['pair_efficiency']}")

    overall = ResultFormatter.build_overall_summary(
        final_plan=plans,
        by_type_stats={'A__O1': stats},
        runtime_stats={
            'group_pack_seconds': 0.5,
            'group_retry_seconds': 0.2,
            'group_repack_seconds': 0.1,
            'group_total_seconds': 0.9,
        },
        total_runtime=1.0,
    )
    assert overall['total_pallets'] == 2
    assert overall['kpi']['failed_near_count'] == 1
    print(f"总体统计 OK: failed_near={overall['kpi']['failed_near_count']}")
    print("[PASS] ResultFormatter\n")


def test_output_quality_gate():
    print("=" * 60)
    print("测试输出质量门禁")
    print("=" * 60)

    raw_boxes = [
        {'id': 1, 'length': 100, 'width': 100, 'height': 100},
        {'id': 2, 'length': 100, 'width': 100, 'height': 100},
    ]
    valid_pallets = [
        {
            'pallet_id': 'P1',
            'packed_items': [
                {
                    'id': 1,
                    'length': 100,
                    'width': 100,
                    'height': 100,
                    'volume': 1000000,
                },
                {
                    'id': 2,
                    'length': 100,
                    'width': 100,
                    'height': 100,
                    'volume': 1000000,
                },
            ],
        }
    ]
    ResultFormatter.validate_output_quality(raw_boxes, valid_pallets)

    invalid_cases = [
        [{'pallet_id': 'EMPTY', 'packed_items': []}],
        [{
            'pallet_id': 'DUP',
            'packed_items': [
                {'id': 1, 'length': 100, 'width': 100, 'height': 100},
                {'id': 1, 'length': 100, 'width': 100, 'height': 100},
            ],
        }],
        [{
            'pallet_id': 'ZERO',
            'packed_items': [
                {'id': 1, 'length': 0, 'width': 100, 'height': 100},
                {'id': 2, 'length': 100, 'width': 100, 'height': 100},
            ],
        }],
    ]
    for pallets in invalid_cases:
        try:
            ResultFormatter.validate_output_quality(raw_boxes, pallets)
        except ValueError:
            continue
        raise AssertionError(f"质量门禁未拦截非法输出: {pallets}")
    print("[PASS] 输出质量门禁\n")


def test_output_fill_rate():
    print("=" * 60)
    print("测试输出填充率")
    print("=" * 60)

    pallet_dims = {'length': 1000, 'width': 1000, 'height': 1000}
    raw_boxes = [
        {'id': 1, 'length': 100, 'width': 100, 'height': 100},
        {'id': 2, 'length': 200, 'width': 100, 'height': 100},
    ]
    plan = [{
        'pallet_id': 'P1',
        'packed_items': [
            {
                'id': 1,
                'length': 100,
                'width': 100,
                'height': 100,
                'position': {'x': 0, 'y': 0, 'z': 0},
                'pallet_dims': pallet_dims,
            },
            {
                'id': 2,
                'length': 200,
                'width': 100,
                'height': 100,
                'position': {'x': 100, 'y': 0, 'z': 0},
                'pallet_dims': pallet_dims,
            },
        ],
    }]

    output = build_json_output_plan(plan, raw_boxes)
    pallet = output[0]
    assert pallet['box_total_volume'] == 3000000
    assert pallet['pallet_volume'] == 1000000000
    assert pallet['fill_rate'] == 0.003
    print(
        f"填充率 OK: {pallet['box_total_volume']}/"
        f"{pallet['pallet_volume']}={pallet['fill_rate']}"
    )
    print("[PASS] 输出填充率\n")


def test_workflow_smoke():
    print("=" * 60)
    print("测试 PackingWorkflow (smoke)")
    print("=" * 60)

    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    boxes = [
        {
            'id': i,
            'pallet_type': 'A',
            'sales_order_no': 'O1',
            'pallet_dims': pallet_dims,
            'min_pack_multiple': 12.0,
            'length': 100,
            'width': 100,
            'height': 100,
        }
        for i in range(1, 4)
    ]

    class StubRescueOptimizer:
        def optimize_failed_by_failed(self, plans, target):
            return {'rescued': 0, 'pair_tried': 0, 'pair_improved': 0}

    class StubPoolRebuilder:
        def rebuild(self, plans, pallet_dims, target):
            return {'rescued': 0, 'rebuild_attempts': 0}

    class StubLowLoadRebuilder:
        def rebuild(self, plans, pallet_dims, target):
            return {'low_load_tried': 0, 'low_load_accepted': 0}

    class StubTailFragmentAbsorber:
        def absorb(self, plans, pallet_dims, target):
            return {'tail_absorb_tried': 0, 'tail_absorb_success': 0}

    class StubLowFillRepacker:
        def repack(self, plans, pallet_dims, target, geometric_target_unreachable=False):
            return {'low_fill_tried': 0, 'low_fill_accepted': 0}

    workflow = PackingWorkflow(
        preprocess_fn=lambda *a, **k: boxes,
        custom_packer_cls=_make_stub_packer(),
        build_direct_layer_solution=lambda *a, **k: [],
        build_centered_single_box_solution=lambda *a, **k: [],
        validate_center_of_mass=lambda sol, d: {'is_stable': True},
        fast_rescue_hole_fill=lambda *a, **k: {'rescued': 0},
        fast_rescue_topup=lambda *a, **k: {'rescued': 0},
        rescue_by_recipe_rebuild=lambda *a, **k: {'rescued': 0},
        rescue_optimizer=StubRescueOptimizer(),
        failed_pool_rebuilder=StubPoolRebuilder(),
        low_fill_repacker=StubLowFillRepacker(),
        tail_fragment_absorber=StubTailFragmentAbsorber(),
        low_load_rebuilder=StubLowLoadRebuilder(),
        make_json_output_plan=lambda plan, raw: plan,
        pallet_index_targets={'A': 20.0},
    )

    report = workflow.run()
    assert report is not None
    assert 'summary' in report
    assert report['summary']['overall']['total_pallets'] >= 1
    print(f"报告 OK: 托盘 {report['summary']['overall']['total_pallets']}")
    print("[PASS] PackingWorkflow smoke\n")


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("主流程模块测试套件")
    print("=" * 60 + "\n")
    try:
        test_order_processor()
        test_report_persister_writes_pallet_excel_summary()
        test_result_formatter()
        test_output_quality_gate()
        test_output_fill_rate()
        test_pallet_packer()
        test_pallet_packer_conservation_fallback()
        test_main_score_prefers_fuller_target_met_pallet()
        test_main_score_prefers_closer_gap_before_future_tail_when_not_met()
        test_main_score_prefers_fuller_target_met_over_exact_but_sparse()
        test_pallet_candidate_pool_includes_fill_items_after_index_target()
        test_target_pack_does_not_stop_when_target_met_by_default()
        test_initial_pack_compares_direct_and_fill_candidates()
        test_main_flow_only_fills_when_future_success_is_preserved()
        test_main_topup_rejects_fill_that_reduces_future_success()
        test_candidate_selection_preserves_success_potential()
        test_workflow_smoke()
        print("=" * 60)
        print("[PASS] 所有测试通过！")
        print("=" * 60)
    except AssertionError as e:
        import traceback
        print(f"\n[FAIL] 测试失败: {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] 测试出错: {e}")
        traceback.print_exc()
        sys.exit(1)
