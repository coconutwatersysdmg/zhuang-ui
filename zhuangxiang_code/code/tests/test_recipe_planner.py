"""配方规划器与配方优先编排测试。"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.main.recipe_planner import (
    build_type_table,
    enumerate_recipes,
    plan_recipe_pools,
)
from src.main.recipe_first import pack_group_recipe_first


PALLET = {'length': 1440, 'width': 2240, 'height': 720}


def _mk(prefix, count, length, width, height, mpm):
    return [
        {
            'id': '%s%d' % (prefix, i),
            'length': length, 'width': width, 'height': height,
            'weight': 1.0, 'min_pack_multiple': mpm,
            'pallet_dims': PALLET,
            'pallet_type': 'MH423C', 'sales_order_no': 'T1',
        }
        for i in range(count)
    ]


def test_planner_group2_like_reaches_12_plus():
    """组2同构库存：480大箱+三种240伴层 → 至少规划出 12 个达标实例。"""
    boxes = (
        _mk('A', 177, 700, 530, 480, 16)
        + _mk('B', 305, 350, 265, 240, 2)
        + _mk('C', 40, 350, 530, 240, 4)
        + _mk('D', 8, 700, 530, 240, 8)
    )
    pools, meta = plan_recipe_pools(boxes, 192, PALLET)
    assert len(pools) >= 12, '应规划 >=12 实例，实际 %d' % len(pools)
    seen = set()
    all_ids = {b['id'] for b in boxes}
    for pool in pools:
        mpm = sum(b['min_pack_multiple'] for b in pool)
        assert mpm + 1e-9 >= 192, '实例 mpm 不足: %g' % mpm
        ids = {b['id'] for b in pool}
        assert not (ids & seen), '实例间箱子重复'
        assert ids <= all_ids, '实例使用了库存外的箱子'
        seen |= ids
    print('[PASS] planner_group2_like: %d 实例，箱子互斥且在库存内' % len(pools))


def test_recipe_use_counts_consistent_with_mpm():
    """配方 use 的箱数 × 单箱 mpm 必须等于配方 mpm。"""
    boxes = _mk('A', 32, 700, 530, 480, 16) + _mk('B', 96, 350, 265, 240, 2)
    types = build_type_table(boxes, PALLET)
    recipes = enumerate_recipes(types, PALLET, 192)
    assert recipes, '应能枚举出配方'
    type_map = {t['idx']: t for t in types}
    for recipe in recipes:
        mpm_from_use = sum(
            type_map[ti]['mpm'] * n for ti, n in recipe['use'].items()
        )
        assert abs(mpm_from_use - recipe['mpm']) < 1e-6, (
            'use 与 mpm 不一致: %r' % recipe
        )
    print('[PASS] recipe_use_counts_consistent: %d 个配方' % len(recipes))


class _StubPacker:
    """pack_group 返回固定基线；_initial_pack 永远失败。"""

    def pack_group(self, pallet_type, sales_order_no, boxes, target_mpm):
        plan = [{
            'pallet_id': 'p1',
            'pallet_type': pallet_type,
            'sales_order_no': sales_order_no,
            'packed_items': list(boxes),
            'mpm_total': sum(b['min_pack_multiple'] for b in boxes),
            'mpm_target': target_mpm,
            'mpm_gap': 0.0,
            'mpm_status': 'FAILED',
            'stability_checks': {'status': 'SUCCESS'},
        }]
        runtime = {'packing': 0.0, 'topup': 0.0, 'retry': 0.0}
        return plan, runtime, {'main_tail_absorb': {}}

    def _initial_pack(self, pool, target_mpm, pallet_dims, counter,
                      fill_aware=False, hard_recipe_diag=None):
        return []


def test_recipe_first_falls_back_when_instances_fail():
    """实装全失败时必须原样返回基线方案（fast 与 safe 模式同此）。"""
    import src.main.recipe_first as rf_mod
    boxes = _mk('A', 24, 700, 530, 240, 8)  # 3 整层 = 192，可规划出 1 实例
    packer = _StubPacker()
    original_layered = rf_mod.try_layered_order
    rf_mod.try_layered_order = lambda *a, **k: None  # 隔离列式优先，测 recipe 路径
    try:
        plan, runtime, diag = pack_group_recipe_first(
            packer, 'MH423C', 'T1', boxes, 192.0
        )
    finally:
        rf_mod.try_layered_order = original_layered
    rf = diag.get('recipe_first', {})
    assert rf.get('recipe_planned', 0) >= 1, '应规划出至少 1 个实例'
    assert rf.get('recipe_adopted') is False, '实装失败不得采用配方方案'
    assert len(plan) == 1 and plan[0]['pallet_id'] == 'p1', '必须原样返回基线'
    print('[PASS] recipe_first_falls_back: 实装失败 → 基线兜底')


class _StubPackerRecipeWins:
    """_initial_pack 整池成功；pack_group 返回失败基线并记录调用。"""

    def __init__(self):
        self.pack_group_calls = 0

    def pack_group(self, pallet_type, sales_order_no, boxes, target_mpm):
        self.pack_group_calls += 1
        plan = [{
            'pallet_id': 'base-1',
            'pallet_type': pallet_type,
            'sales_order_no': sales_order_no,
            'packed_items': list(boxes),
            'mpm_total': sum(b['min_pack_multiple'] for b in boxes),
            'mpm_target': target_mpm,
            'mpm_gap': 0.0,
            'mpm_status': 'FAILED',
            'stability_checks': {'status': 'SUCCESS'},
        }]
        runtime = {'packing': 0.0, 'topup': 0.0, 'retry': 0.0}
        return plan, runtime, {'main_tail_absorb': {}}

    def _initial_pack(self, pool, target_mpm, pallet_dims, counter,
                      fill_aware=False, hard_recipe_diag=None):
        packed = []
        for i, box in enumerate(pool):
            item = dict(box)
            item['position'] = {'x': 0, 'y': 0, 'z': i}
            packed.append(item)
        return packed


def test_fast_mode_skips_baseline_when_recipe_succeeds(monkeypatch=None):
    """fast 模式下配方路径成功（整组无剩余）时不得调用基线 pack_group。"""
    import src.main.recipe_first as rf_mod

    boxes = _mk('A', 24, 700, 530, 240, 8)  # 恰好 1 个 192 实例，无剩余箱
    packer = _StubPackerRecipeWins()

    original_gate = rf_mod.validate_pallet_constraints
    original_layered = rf_mod.try_layered_order
    rf_mod.validate_pallet_constraints = (
        lambda plan, dims, **kwargs: {'is_valid': True, 'violations': []}
    )
    rf_mod.try_layered_order = lambda *a, **k: None  # 隔离列式优先，测 recipe 路径
    try:
        plan, runtime, diag = pack_group_recipe_first(
            packer, 'MH423C', 'T1', boxes, 192.0, safe_compare=False
        )
    finally:
        rf_mod.validate_pallet_constraints = original_gate
        rf_mod.try_layered_order = original_layered

    rf = diag.get('recipe_first', {})
    assert rf.get('recipe_mode') == 'fast'
    assert rf.get('recipe_adopted') is True
    assert packer.pack_group_calls == 0, (
        'fast 模式整组被配方覆盖时不应跑基线，实际调用 %d 次'
        % packer.pack_group_calls
    )
    assert len(plan) == 1 and plan[0]['mpm_status'] == 'SUCCESS'
    out_ids = {b['id'] for sol in plan for b in sol['packed_items']}
    assert out_ids == {b['id'] for b in boxes}, '箱子必须守恒'
    print('[PASS] fast_mode_skips_baseline: 配方成功 → 0 次基线调用')


def test_safe_mode_still_runs_baseline():
    """safe 模式必须仍跑基线并按棘轮对比。"""
    import src.main.recipe_first as rf_mod

    boxes = _mk('A', 24, 700, 530, 240, 8)
    packer = _StubPackerRecipeWins()

    original_gate = rf_mod.validate_pallet_constraints
    original_layered = rf_mod.try_layered_order
    rf_mod.validate_pallet_constraints = (
        lambda plan, dims, **kwargs: {'is_valid': True, 'violations': []}
    )
    rf_mod.try_layered_order = lambda *a, **k: None  # 隔离列式优先（safe 模式本不触发）
    try:
        plan, runtime, diag = pack_group_recipe_first(
            packer, 'MH423C', 'T1', boxes, 192.0, safe_compare=True
        )
    finally:
        rf_mod.validate_pallet_constraints = original_gate
        rf_mod.try_layered_order = original_layered

    rf = diag.get('recipe_first', {})
    assert rf.get('recipe_mode') == 'safe'
    assert packer.pack_group_calls == 1, 'safe 模式必须跑 1 次基线'
    assert rf.get('recipe_adopted') is True, '配方达标更多应被采用'
    assert plan[0]['mpm_status'] == 'SUCCESS'
    print('[PASS] safe_mode_still_runs_baseline: 双跑棘轮保留')


if __name__ == '__main__':
    test_planner_group2_like_reaches_12_plus()
    test_recipe_use_counts_consistent_with_mpm()
    test_recipe_first_falls_back_when_instances_fail()
    test_fast_mode_skips_baseline_when_recipe_succeeds()
    test_safe_mode_still_runs_baseline()
    print('[PASS] 所有测试通过！')
