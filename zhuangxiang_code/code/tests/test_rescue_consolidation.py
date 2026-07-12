"""失败托盘互借修复（合并装满 + 指数再分配）专项测试。

验证 RescueOptimizer.optimize_failed_by_failed 实装后的核心语义：
1. 两个半空失败盘 → 合并为一个更满的盘（守恒、门禁、盘数减少）；
2. 高填充 SUCCESS 盘绝不被触碰（低填充达标盘可被"溶解"参与再分配）；
3. 指数可凑够时顺带产出新达标盘（rescued>0）；
4. 已够满的失败盘不进池（保持不变）；
5. 预算耗尽降级收尾，已凑出的达标盘不丢（慢机器回归修复）；
6. 指数再分配：低填充达标盘溶解进池 → 净增达标（棘轮保证不净亏）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig
from src.geometry import validate_center_of_mass
from src.packing import BeamSearchPacker
from src.rescue import RescueOptimizer
from src.rescue.pallet_evaluator import PalletEvaluator

PALLET = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}
TARGET = 192.0


def _make_optimizer() -> RescueOptimizer:
    return RescueOptimizer(
        pallet_dims=PALLET,
        custom_packer_cls=BeamSearchPacker,
        validate_center_of_mass=validate_center_of_mass,
        constraint_config=ConstraintConfig(),
    )


def _grid_plan(pallet_id: str, n_boxes: int, id_prefix: str,
               mpm: float = 2.0) -> dict:
    """构造一个合法网格摆放的失败盘：350×265×240 箱，4 列/层×最多 8 行。"""
    items = []
    per_row, per_col = 4, 8          # x 方向 4 个(352*4=1408)，y 方向 8 个(267*8=2136)
    for k in range(n_boxes):
        layer = k // (per_row * per_col)
        rem = k % (per_row * per_col)
        row, col = rem // per_row, rem % per_row
        items.append({
            'id': f'{id_prefix}-{k:03d}',
            'type': 'T1',
            'length': 352.0, 'width': 267.0, 'height': 240.0,
            'raw_length': 350.0, 'raw_width': 265.0, 'raw_height': 240.0,
            'position': {'x': col * 352.0, 'y': row * 267.0,
                         'z': layer * 240.0},
            'weight': 1.0,
            'min_pack_multiple': mpm,
            'is_small_box': False,
            'volume': 350.0 * 265.0 * 240.0,
            'pallet_dims': dict(PALLET),
            'supported_area': 352.0 * 267.0,
            'support_ratio': 1.0,
        })
    plan = {
        'pallet_id': pallet_id,
        'pallet_type': 'MH423C',
        'sales_order_no': 'ORD1',
        'packed_items': items,
        'mpm_target': TARGET,
    }
    PalletEvaluator.calc_pallet_status(plan)
    return plan


def test_merge_two_half_empty():
    """两个 24 箱失败盘（各 fill≈29%）→ 合并为 1 盘，守恒。"""
    plans = [_grid_plan('P-1', 24, 'A'), _grid_plan('P-2', 24, 'B')]
    all_ids = {i['id'] for p in plans for i in p['packed_items']}
    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)
    assert diag['consolidate_accepted'] == 1, diag['consolidate_reason']
    assert len(plans) == 1, '48 箱(1.5 层)应合并进 1 盘'
    out_ids = {i['id'] for p in plans for i in p['packed_items']}
    assert out_ids == all_ids, '箱子守恒'
    assert plans[0].get('mpm_status') == 'FAILED', '96 指数仍未达标'
    print('[PASS] 半空失败盘合并（2 盘 -> 1 盘、守恒）')


def _packed_plan(pallet_id: str, id_prefix: str, n_boxes: int,
                 dims=(700.0, 530.0, 360.0), mpm: float = 12.0) -> dict:
    """用真实装箱器构造托盘（带完整吸盘/支撑字段，能过全量门禁）。"""
    L, W, H = dims
    boxes = [{
        'id': f'{id_prefix}-{k:03d}', 'type': f'T{int(L)}',
        'length': L, 'width': W, 'height': H, 'weight': 5.0,
        'min_pack_multiple': mpm, 'is_small_box': False,
        'volume': L * W * H, 'pallet_dims': dict(PALLET),
    } for k in range(n_boxes)]
    packer = BeamSearchPacker(PALLET, constraint_config=ConstraintConfig())
    packed, unfitted = packer.pack(
        boxes, num_restarts=4, beam_width=3, candidate_limit=10,
        random_seed=7, target_mpm=TARGET, stop_when_target_met=False,
        allow_skip_items=False,
    )
    assert not unfitted and len(packed) == n_boxes, '测试前置：应全部放入'
    plan = {
        'pallet_id': pallet_id, 'pallet_type': 'MH423C',
        'sales_order_no': 'ORD1', 'packed_items': packed,
        'mpm_target': TARGET,
    }
    PalletEvaluator.calc_pallet_status(plan)
    return plan


def test_success_pallets_untouched():
    """SUCCESS 盘不进池：合并只发生在 FAILED 盘之间。"""
    ok = _packed_plan('P-OK', 'S', 16)     # 16×12=192 达标（700×530×360 两层）
    assert ok['mpm_status'] == 'SUCCESS'
    ok_items = ok['packed_items']          # 引用捕获：验证对象未被替换
    plans = [ok, _grid_plan('P-1', 20, 'A'), _grid_plan('P-2', 20, 'B')]
    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)
    assert diag['consolidate_accepted'] == 1, diag['consolidate_reason']
    assert ok in plans and ok['packed_items'] is ok_items, 'SUCCESS 盘原样保留'
    assert len(plans) == 2, '达标盘 1 + 合并后失败盘 1'
    print('[PASS] SUCCESS 盘不被触碰')


def test_rescue_new_success():
    """两个 8 箱失败盘（各指数 96）→ 凑标 16 箱=192 → 新增达标。

    凑标收益经"局部提交"通道落袋（extract_commit_success），
    不再依赖全池合并通道。
    """
    plans = [_packed_plan('P-1', 'A', 8), _packed_plan('P-2', 'B', 8)]
    assert all(p['mpm_status'] == 'FAILED' for p in plans)
    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)
    assert (diag.get('extract_commit_success', 0) >= 1
            or diag['consolidate_accepted'] == 1), diag['consolidate_reason']
    statuses = [p.get('mpm_status') for p in plans]
    assert diag['rescued'] >= 1 and 'SUCCESS' in statuses, \
        f'应有新达标盘: {statuses}, rescued={diag["rescued"]}'
    all_ids = {f'A-{k:03d}' for k in range(8)} | {f'B-{k:03d}' for k in range(8)}
    out_ids = {i['id'] for p in plans for i in p['packed_items']}
    assert out_ids == all_ids, '箱子守恒'
    print('[PASS] 互借凑指数（凑标局部提交产出新达标盘）')


def test_full_failed_pallets_kept():
    """已够满（fill≥阈值）的失败盘不进池：无第二个可合并盘时不动。"""
    full_a = _grid_plan('P-1', 95, 'A', mpm=1.0)   # fill≈92.3%，指数 95 未达标
    full_b = _grid_plan('P-2', 95, 'B', mpm=1.0)
    before = [list(p['packed_items']) for p in (full_a, full_b)]
    plans = [full_a, full_b]
    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)
    assert diag['consolidate_accepted'] == 0
    assert diag['consolidate_reason'] == 'less_than_2_consolidatable'
    assert [p['packed_items'] for p in plans] == before, '原方案零改动'
    print('[PASS] 够满失败盘不重装')


def test_deadline_degrade_keeps_target_sets():
    """预算耗尽降级收尾：已凑出的达标盘不丢（慢机器回归修复）。

    把预算压到 0（凑标阶段一进循环即超时、重装全程 fast_only），
    合并仍应正常完成且不返回 aborted——降级只影响装满质量。
    """
    plans = [_packed_plan('P-1', 'A', 8), _packed_plan('P-2', 'B', 8)]
    all_ids = {i['id'] for p in plans for i in p['packed_items']}
    opt = _make_optimizer()
    opt.CONSOLIDATION_TIME_BUDGET_S = 0.0   # 立即超时 → 全程降级
    diag = opt.optimize_failed_by_failed(plans, TARGET)
    assert diag['consolidate_reason'] != 'aborted_no_improvement_possible', \
        '超时不得整体放弃'
    out_ids = {i['id'] for p in plans for i in p['packed_items']}
    assert out_ids == all_ids, '箱子守恒'
    print('[PASS] 预算耗尽降级收尾（不整体放弃、守恒）')


def _plan_from_boxes(pallet_id: str, boxes: list) -> dict:
    """真实 beam 装箱一组给定箱子成盘（过全量门禁的摆放）。"""
    packer = BeamSearchPacker(PALLET, constraint_config=ConstraintConfig())
    packed, unfitted = packer.pack(
        boxes, num_restarts=4, beam_width=3, candidate_limit=10,
        random_seed=11, target_mpm=TARGET, stop_when_target_met=False,
        allow_skip_items=False,
    )
    assert not unfitted, '测试前置：应全部放入'
    plan = {
        'pallet_id': pallet_id, 'pallet_type': 'MH423C',
        'sales_order_no': 'ORD1', 'packed_items': packed,
        'mpm_target': TARGET,
    }
    PalletEvaluator.calc_pallet_status(plan)
    return plan


def _mk_boxes(prefix, n, dims, mpm):
    L, W, H = dims
    return [{
        'id': f'{prefix}-{k:03d}', 'type': f'T{prefix}',
        'length': L, 'width': W, 'height': H, 'weight': 5.0,
        'min_pack_multiple': mpm, 'is_small_box': False,
        'volume': L * W * H, 'pallet_dims': dict(PALLET),
    } for k in range(n)]


def test_redistribute_low_fill_success():
    """指数再分配：低填充达标盘溶解 + 失败盘重规划 → 净增达标。

    构造（用户观察到的真实模式：填充 70% 就达标 = 指数分配不合理）：
    - A = 700×530×120 mpm16（高指数密度小薄箱）：8 箱整层 128 + 顶带
      4 箱 64；donor 盘 = 12×A = 192 达标但填充仅 ~23%；
    - B = 700×530×300 mpm8：整层 64，最多 2 层 = 128 < 192 → 纯 B
      几何永不达标；3 个失败盘各 16×B（fill≈77%，指数 128）。
    混合重规划解锁「2 层 B + 顶带 4A = 192」：3 实例恰好用尽
    12A + 48B → 4 盘变 3 盘全达标（净增 +2）。
    棘轮不变式：总达标数绝不下降；若接受则必须净增。
    """
    donor = _plan_from_boxes(
        'P-D', _mk_boxes('A', 12, (700.0, 530.0, 120.0), 16.0))
    assert donor['mpm_status'] == 'SUCCESS', donor.get('mpm_total')
    fails = [
        _plan_from_boxes(
            f'P-F{i}', _mk_boxes(f'B{i}', 16, (700.0, 530.0, 300.0), 8.0))
        for i in range(3)
    ]
    assert all(p['mpm_status'] == 'FAILED' for p in fails)
    plans = [donor] + fails
    all_ids = {i['id'] for p in plans for i in p['packed_items']}
    succ_before = sum(1 for p in plans if p['mpm_status'] == 'SUCCESS')

    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)

    out_ids = {i['id'] for p in plans for i in p['packed_items']}
    assert out_ids == all_ids, '箱子守恒'
    succ_after = sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS')
    assert succ_after >= succ_before, \
        f'棘轮不变式：达标数不得下降 {succ_before}->{succ_after}'
    assert diag.get('redistribute_dissolved', 0) >= 1, \
        f'低填充达标盘应被选为溶解候选: {diag}'
    assert diag.get('redistribute_accepted') == 1, \
        f'应接受再分配: reason={diag.get("redistribute_reason")}'
    assert succ_after > succ_before, '接受则必须净增达标'
    print(f'[PASS] 指数再分配（达标 {succ_before} -> {succ_after}，'
          f'{len(plans)} 盘）')


def test_redistribute_no_gain_skips():
    """预判无净增益时秒退：高填充达标盘（≥80%）不做溶解候选。"""
    ok = _packed_plan('P-OK', 'S', 16)     # fill≈92% 达标 → 不可溶解
    fails = [_grid_plan('P-1', 20, 'A'), _grid_plan('P-2', 20, 'B')]
    plans = [ok] + fails
    ok_items = ok['packed_items']
    diag = _make_optimizer().optimize_failed_by_failed(plans, TARGET)
    assert diag.get('redistribute_dissolved', 0) == 0
    assert diag.get('redistribute_reason') == 'no_low_fill_success_donor'
    assert ok in plans and ok['packed_items'] is ok_items, '高填充达标盘不被触碰'
    print('[PASS] 无低填充达标盘时再分配零开销跳过')


def test_fill_compact_pairwise_fallback():
    """装满压实兜底：整池合并被拒后，碎片盘仍被两两合并吸收。

    模拟真实缺陷（报告中残留 fill 0.07/0.14 失败盘）：直接调用
    _fill_compact（跳过阶段一/二/三），一个 4 箱碎片盘 + 一个 24 箱
    半满盘应合并为 1 盘（守恒、门禁、盘数-1）。
    """
    frag = _grid_plan('P-FRAG', 4, 'F', mpm=1.0)     # fill≈3.8%
    half = _grid_plan('P-HALF', 24, 'H', mpm=1.0)    # fill≈29%
    plans = [frag, half]
    all_ids = {i['id'] for p in plans for i in p['packed_items']}
    opt = _make_optimizer()
    diag = {"fill_compact_merges": 0, "fill_compact_reason": ""}
    opt._fill_compact(plans, TARGET, diag)
    assert diag['fill_compact_merges'] == 1, diag
    assert len(plans) == 1, '碎片盘应被吸收（2 盘 -> 1 盘）'
    out_ids = {i['id'] for p in plans for i in p['packed_items']}
    assert out_ids == all_ids, '箱子守恒'
    print('[PASS] 装满压实兜底（碎片盘两两合并吸收）')


def test_fill_compact_no_room_untouched():
    """体积上并不下时不动：两个 92%+ 满盘无可合并对，零改动。"""
    a = _grid_plan('P-1', 95, 'A', mpm=1.0)
    b = _grid_plan('P-2', 95, 'B', mpm=1.0)
    plans = [a, b]
    before = [list(p['packed_items']) for p in plans]
    opt = _make_optimizer()
    diag = {"fill_compact_merges": 0, "fill_compact_reason": ""}
    opt._fill_compact(plans, TARGET, diag)
    assert diag['fill_compact_merges'] == 0
    assert [p['packed_items'] for p in plans] == before, '原方案零改动'
    print('[PASS] 满盘无可合并对时装满压实零改动')


if __name__ == '__main__':
    test_merge_two_half_empty()
    test_success_pallets_untouched()
    test_rescue_new_success()
    test_full_failed_pallets_kept()
    test_deadline_degrade_keeps_target_sets()
    test_redistribute_low_fill_success()
    test_redistribute_no_gain_skips()
    test_fill_compact_pairwise_fallback()
    test_fill_compact_no_room_untouched()
    print('\n[PASS] 互借修复（合并装满+指数再分配+装满压实）全部测试通过！')
