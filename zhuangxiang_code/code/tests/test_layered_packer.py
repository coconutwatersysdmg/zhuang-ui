"""列式装箱器测试：单托盘达标、守恒、门禁通过、旋转朝向、边界回落。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig
from src.packing.layered_packer import try_layered_order

PALLET = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}


class _Packer:
    """最小 packer：仅提供 _cfg，layered 据此构造 BeamSearchPacker helper。"""

    def __init__(self):
        self._cfg = ConstraintConfig()


def _mk(prefix, count, length, width, height, mpm):
    return [
        {
            'id': '%s%d' % (prefix, i), 'length': length, 'width': width,
            'height': height, 'weight': 1.0, 'min_pack_multiple': mpm,
            'is_small_box': False, 'pallet_dims': PALLET,
        }
        for i in range(count)
    ]


def test_single_footprint_rotated_reaches_target():
    """16×(530×350×480)+16×(530×350×240)：须旋转成 350 沿 x 才能单托盘达标。"""
    boxes = _mk('A', 16, 530, 350, 480, 8) + _mk('B', 16, 530, 350, 240, 4)
    assert sum(b['min_pack_multiple'] for b in boxes) == 192
    plan = try_layered_order(_Packer(), boxes, 192.0, PALLET)
    assert plan is not None, '应能列式装出达标方案'
    assert len(plan) == 1 and plan[0]['mpm_status'] == 'SUCCESS'
    assert plan[0]['mpm_total'] + 1e-9 >= 192
    assert {b['id'] for b in plan[0]['packed_items']} == {b['id'] for b in boxes}
    for it in plan[0]['packed_items']:
        assert it.get('layered_oriented') is True, '须打旋转保留标记'
    print('[PASS] 单底面旋转达标 + 守恒 + 旋转标记')


def test_multi_footprint_reaches_target():
    """多底面混合（530×350 两种高 + 350×265）单托盘达标（ANALYSIS_004 型）。"""
    boxes = (
        _mk('A', 8, 530, 350, 360, 6)
        + _mk('B', 18, 530, 350, 240, 4)
        + _mk('C', 36, 350, 265, 240, 2)
    )
    assert sum(b['min_pack_multiple'] for b in boxes) == 192
    plan = try_layered_order(_Packer(), boxes, 192.0, PALLET)
    assert plan is not None and plan[0]['mpm_status'] == 'SUCCESS'
    assert {b['id'] for b in plan[0]['packed_items']} == {b['id'] for b in boxes}
    print('[PASS] 多底面混合达标 + 守恒')


def test_non_single_pallet_returns_none():
    """整单指数远超单托盘（≥target×1.5）时不介入，返回 None 回落。"""
    boxes = _mk('A', 100, 530, 350, 240, 4)  # 400 = 2.08×192
    assert try_layered_order(_Packer(), boxes, 192.0, PALLET) is None
    print('[PASS] 多托盘场景回落 None')


def test_unmatched_footprint_returns_none():
    """底面与列宽网格不匹配（无 350/265/700 公共边）时回落 None。"""
    boxes = _mk('A', 6, 600, 600, 240, 32)  # 600×600 非规整底面
    assert try_layered_order(_Packer(), boxes, 192.0, PALLET) is None
    print('[PASS] 非规整底面回落 None')


if __name__ == '__main__':
    test_single_footprint_rotated_reaches_target()
    test_multi_footprint_reaches_target()
    test_non_single_pallet_returns_none()
    test_unmatched_footprint_returns_none()
    print('\n[PASS] 所有列式装箱测试通过！')
