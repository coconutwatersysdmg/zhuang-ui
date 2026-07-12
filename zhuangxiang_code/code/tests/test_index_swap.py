# -*- coding: utf-8 -*-
"""指数互换救援（成功盘↔失败盘）单元测试。

用真实 BeamSearchPacker 构造托盘（保证吸盘字段与门禁可过），验证：
1. 富余成功盘捐出高指数箱 → 失败盘凑标，双双达标、守恒；
2. 无富余（σ=0）时诚实不动；
3. 富余不足覆盖缺口时诚实不动。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config.constraint_config import ConstraintConfig
from src.geometry.center_of_mass import validate_center_of_mass
from src.packing.beam_search_packer import BeamSearchPacker
from src.rescue.index_swap import swap_success_failed
from src.rescue.pallet_evaluator import PalletEvaluator

PALLET = {'length': 1440, 'width': 2240, 'height': 720}
TARGET = 192.0
CFG = ConstraintConfig()


def _packed_plan(pallet_id, id_prefix, n_boxes, mpm, dims=(700, 530, 360)):
    """用真实装箱器构造一个托盘方案（箱字段齐全、门禁可过）。"""
    l, w, h = dims
    boxes = [
        {
            'id': f'{id_prefix}{i}',
            'type': f'T{l}x{w}',
            'length': l, 'width': w, 'height': h,
            'weight': 5.0,
            'min_pack_multiple': mpm,
            'pallet_type': 'MH423C',
            'sales_order_no': 'SWAP_TEST',
        }
        for i in range(n_boxes)
    ]
    packer = BeamSearchPacker(
        PALLET,
        support_ratio_threshold=CFG.support_ratio_threshold,
        size_tolerance=2.0,
        constraint_config=CFG,
    )
    packed, unfitted = packer.pack(
        boxes, num_restarts=2, beam_width=2, candidate_limit=8,
        random_seed=7, target_mpm=None, stop_when_target_met=False,
        allow_skip_items=False,
    )
    assert not unfitted, f'构造托盘失败：{len(unfitted)} 箱未装入'
    plan = {
        'pallet_id': pallet_id,
        'pallet_type': 'MH423C',
        'sales_order_no': 'SWAP_TEST',
        'packed_items': packed,
        'mpm_target': TARGET,
    }
    PalletEvaluator.calc_pallet_status(plan)
    return plan


def _ids(plans):
    return sorted(
        str(i.get('id')) for p in plans for i in p.get('packed_items', [])
    )


def test_swap_rescues_failed_pallet():
    """donor 208(σ=16) 捐一个 16 指数箱 → receiver 176 凑到 192。"""
    donor = _packed_plan('D1', 'd', 13, 16.0)      # 13×16 = 208
    recv = _packed_plan('R1', 'r', 11, 16.0)       # 11×16 = 176，缺 16
    plans = [donor, recv]
    before_ids = _ids(plans)
    diag = swap_success_failed(
        plans, TARGET, PALLET, BeamSearchPacker,
        validate_center_of_mass, CFG,
    )
    assert diag['swap_accepted'] == 1, diag
    for p in plans:
        PalletEvaluator.calc_pallet_status(p)
        assert p['mpm_status'] == 'SUCCESS', (
            p['pallet_id'], p.get('mpm_total'))
    assert _ids(plans) == before_ids  # 两盘并集守恒
    assert len(donor['packed_items']) == 12
    assert len(recv['packed_items']) == 12
    print('[PASS] 富余捐赠互换：failed 176 -> 192，donor 保持达标')


def test_swap_no_surplus_noop():
    """全部成功盘恰好 192（σ=0）→ 数学上不可换，诚实不动。"""
    donor = _packed_plan('D1', 'd', 12, 16.0)      # 192 整
    recv = _packed_plan('R1', 'r', 11, 16.0)       # 176
    plans = [donor, recv]
    before_ids = _ids(plans)
    diag = swap_success_failed(
        plans, TARGET, PALLET, BeamSearchPacker,
        validate_center_of_mass, CFG,
    )
    assert diag['swap_accepted'] == 0
    assert diag['swap_reason'] == 'no_surplus_donor', diag
    assert _ids(plans) == before_ids
    print('[PASS] σ=0 无富余：诚实不动')


def test_swap_surplus_insufficient_noop():
    """富余 σ=16 < 缺口 32 → 盖不住，诚实不动。"""
    donor = _packed_plan('D1', 'd', 13, 16.0)      # 208，σ=16
    recv = _packed_plan('R1', 'r', 10, 16.0)       # 160，缺 32
    plans = [donor, recv]
    diag = swap_success_failed(
        plans, TARGET, PALLET, BeamSearchPacker,
        validate_center_of_mass, CFG,
    )
    assert diag['swap_accepted'] == 0, diag
    assert diag['swap_surplus_total'] == 16.0
    print('[PASS] 富余不足：诚实不动')


if __name__ == '__main__':
    print('=' * 60)
    print('指数互换救援测试')
    print('=' * 60)
    try:
        test_swap_rescues_failed_pallet()
        test_swap_no_surplus_noop()
        test_swap_surplus_insufficient_noop()
        print('=' * 60)
        print('[PASS] 指数互换全部测试通过！')
        print('=' * 60)
    except AssertionError as e:
        print(f'[FAIL] {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
