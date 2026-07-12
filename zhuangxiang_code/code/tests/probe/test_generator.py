"""生成器不变量单测：Σmpm=192×N、0 残料、几何可达标、旋转敏感标记正确。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.probe import generator as g  # noqa: E402


def test_archetype_invariants():
    """每个可达标原型满盘指数严格 = 192；不可达标原型 < 192。"""
    for make in (g.reg_multilayer, g.reg_wide, g.reg_big,
                 g.single_layer, g.big_mpm, g.rotation_sensitive):
        a = make().validate()
        assert abs(a.pallet_mpm_best - g.TARGET) < 1e-9, a.label
    blow = g.big_low_unreachable().validate()
    assert blow.pallet_mpm_best < g.TARGET
    print('[PASS] 原型满盘指数不变量')


def test_rotation_sensitive_crosses_target():
    """旋转敏感原型：固定满盘 <192 ≤ 旋转满盘。"""
    a = g.rotation_sensitive()
    assert a.pallet_mpm_fixed < g.TARGET <= a.pallet_mpm_best
    assert a.pallet_mpm_fixed == 144 and a.pallet_mpm_best == 192, (
        a.pallet_mpm_fixed, a.pallet_mpm_best)
    print(f'[PASS] 旋转敏感：固定 {a.pallet_mpm_fixed} < 192 = 旋转 {a.pallet_mpm_best}')


def test_order_conservation():
    """订单总指数 = 192 × n_optimal，且箱 id 唯一。"""
    o = g.build_order('mix', [
        (g.reg_multilayer(), 2), (g.reg_wide(), 2), (g.reg_big(), 1)])
    assert o.n_optimal == 5
    reach_mpm = sum(b['min_pack_multiple'] for b in o.boxes)
    assert abs(reach_mpm - g.TARGET * 5) < 1e-6, reach_mpm
    ids = [b['id'] for b in o.boxes]
    assert len(ids) == len(set(ids)), '箱 id 唯一'
    # 每盘箱数 × 盘数守恒
    assert len(o.boxes) == 96 * 2 + 48 * 2 + 24 * 1
    print(f'[PASS] 订单守恒：{len(o.boxes)} 箱 = 192×5 指数 = {reach_mpm:g}')


def test_straddle_has_unreachable():
    """阈值跨越订单含不可达标盘，N 只数好盘。"""
    o = g.build_order('s', [
        (g.reg_multilayer(), 2), (g.big_low_unreachable(), 3)])
    assert o.n_optimal == 2 and o.n_unreachable == 3
    print('[PASS] 阈值跨越 N=好盘数，坏盘单列')


if __name__ == '__main__':
    test_archetype_invariants()
    test_rotation_sensitive_crosses_target()
    test_order_conservation()
    test_straddle_has_unreachable()
    print('\n[PASS] 生成器全部不变量通过！')
