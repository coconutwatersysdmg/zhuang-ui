"""全局列式装箱器(GCP)测试：达标、守恒、门禁、自适应判定、契约。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig
from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.global_column_packer import GlobalColumnPacker

PALLET = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}


def _mk(prefix, count, length, width, height, mpm):
    return [
        {
            'id': '%s%d' % (prefix, i), 'length': length, 'width': width,
            'height': height, 'weight': 1.0, 'min_pack_multiple': mpm,
            'is_small_box': False, 'pallet_dims': PALLET,
        }
        for i in range(count)
    ]


def _regular_order():
    """规则单订单：288×(350×265×240)=576 指数 → 3 个满盘（每盘 32 柱×6=192）。"""
    return _mk('A', 288, 350, 265, 240, 2)


def test_contract_and_conservation():
    """pack_group 返回契约 + 守恒 + 门禁。"""
    boxes = _regular_order()
    gcp = GlobalColumnPacker(constraint_config=ConstraintConfig())
    plan, runtime, diag = gcp.pack_group('MH423C', 'T', boxes, 192.0)
    assert isinstance(plan, list) and 'packing' in runtime
    out_ids = [b['id'] for p in plan for b in p['packed_items']]
    assert set(out_ids) == {b['id'] for b in boxes}, '守恒：箱 id 一致'
    assert len(out_ids) == len(boxes), '守恒：无重复无丢失'
    for p in plan:
        assert {'pallet_id', 'pallet_type', 'sales_order_no', 'packed_items',
                'mpm_total', 'mpm_status'} <= set(p.keys())
        g = validate_pallet_constraints(
            {'packed_items': p['packed_items']}, PALLET, constraint_config=ConstraintConfig())
        assert g['is_valid'], f"盘门禁须过：{g['violations'][:2]}"
    print('[PASS] 契约 + 守恒 + 逐盘门禁')


def test_regular_reaches_target():
    """规则单订单应全部达标（单一底面满盘）。"""
    boxes = _regular_order()
    gcp = GlobalColumnPacker(constraint_config=ConstraintConfig())
    plan, _, _ = gcp.pack_group('MH423C', 'T', boxes, 192.0)
    succ = sum(1 for p in plan if p['mpm_status'] == 'SUCCESS')
    assert succ >= 3, f'96 箱应出 3 个达标盘，实际 {succ}'
    print(f'[PASS] 规则单订单达标 {succ} 盘')


def test_suits_group():
    """自适应判定：规则数据 True；底面大指数低的非满柱数据 False。"""
    gcp = GlobalColumnPacker(constraint_config=ConstraintConfig())
    assert gcp.suits_group(_regular_order(), 192.0) is True
    # 底面大、指数低（满盘都不够 192）→ 不适合
    big_low = _mk('B', 40, 700, 530, 240, 4)  # 700×530 满盘根数少 × mpm4 < 192
    assert gcp.suits_group(big_low, 192.0) is False
    print('[PASS] 自适应判定（规则→GCP / 非满柱大底面→回退）')


def test_no_target_returns_plan():
    """无目标指数时仍出盘且守恒（退化为尽量装）。"""
    boxes = _mk('C', 32, 350, 265, 240, 2)
    gcp = GlobalColumnPacker(constraint_config=ConstraintConfig())
    plan, _, _ = gcp.pack_group('MH423C', 'T', boxes, None)
    out_ids = [b['id'] for p in plan for b in p['packed_items']]
    assert set(out_ids) == {b['id'] for b in boxes}
    print('[PASS] 无目标也守恒出盘')


if __name__ == '__main__':
    test_contract_and_conservation()
    test_regular_reaches_target()
    test_suits_group()
    test_no_target_returns_plan()
    print('\n[PASS] 所有 GCP 测试通过！')
