"""泛化探测回归护栏（快速子集，仅 GCP 廉价场景 + 旋转缺陷护栏）。

锁定「构造已知最优」场景的 S/N=1.0，防未来改动悄悄退步；并护住
「baseline 固定朝向是缺陷、默认开旋转修复」这一结论。

耗时的 beam 全量与异质柱 ILP 压测不在此（见 grid_scan）；balanced_mpm
是已知盲区（GCP 1/4），不在护栏内。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.probe import generator as g  # noqa: E402
from tests.probe.runner import run_order  # noqa: E402


def _assert_optimal(order, main_packer='gcp', allow_rotation=True):
    m = run_order(order, main_packer=main_packer, allow_rotation=allow_rotation)
    assert m.gate_ok, f'{order.name}: 门禁违例 {m.error}'
    assert m.conserved, f'{order.name}: 守恒失败'
    assert m.s_median == m.n_optimal, (
        f'{order.name} [{main_packer}]: S/N={m.s_median}/{m.n_optimal} 非最优')
    return m


def test_gcp_reaches_optimal_on_reachable():
    """GCP 在可达标构造订单上达最优（footprint 多样 / 单层 / 混高 / 大mpm）。"""
    _assert_optimal(g.build_order('reg', [(g.reg_multilayer(), 2)]))
    _assert_optimal(g.build_order('multi', [
        (g.reg_multilayer(), 1), (g.reg_wide(), 1), (g.reg_big(), 1)]))
    _assert_optimal(g.build_order('single', [(g.single_layer(), 2)]))
    _assert_optimal(g.build_order('mixh', [
        (g.reg_multilayer(), 1), (g.single_layer(), 1)]))
    _assert_optimal(g.build_order('bigm', [(g.big_mpm(), 2)]))
    print('[PASS] GCP 可达标场景全达最优')


def test_rotation_default_on_reaches_optimal():
    """旋转敏感订单：GCP 与 baseline(默认开旋转) 均达最优。"""
    rot = g.build_order('rot', [(g.rotation_sensitive(), 2)])
    _assert_optimal(rot, main_packer='gcp')
    _assert_optimal(rot, main_packer='beam', allow_rotation=True)
    print('[PASS] 旋转敏感：GCP / baseline 开旋转均达最优')


def test_high_density_reaches_optimal():
    """超高密度(≈99% 填充)可达标订单：GCP 仍达最优(密栅 + 混底面)。"""
    _assert_optimal(g.build_order('dA', [(g.dense_grid_a(), 2)]))
    _assert_optimal(g.build_order('dB', [(g.dense_grid_b(), 2)]))
    _assert_optimal(g.build_dense_mixed_order('dM', 2))
    print('[PASS] 超高密度 ≈99% 填充仍达最优')


def test_fixed_orientation_is_a_defect():
    """护栏：baseline 关旋转（固定朝向）在旋转敏感订单上达不到最优。

    证明「固定朝向是缺陷、旋转开关有意义」；若此断言失效，说明旋转逻辑
    被改动或失效，需复查。
    """
    rot = g.build_order('rot', [(g.rotation_sensitive(), 2)])
    m = run_order(rot, main_packer='beam', allow_rotation=False)
    assert m.conserved and m.gate_ok, '固定朝向也须守恒+门禁通过'
    assert m.s_median < m.n_optimal, (
        f'固定朝向竟达最优 {m.s_median}/{m.n_optimal}？旋转逻辑可能变化，请复查')
    print(f'[PASS] 固定朝向确为缺陷：S/N={m.s_median}/{m.n_optimal}')


if __name__ == '__main__':
    test_gcp_reaches_optimal_on_reachable()
    test_rotation_default_on_reaches_optimal()
    test_high_density_reaches_optimal()
    test_fixed_orientation_is_a_defect()
    print('\n[PASS] 泛化探测回归护栏全部通过！')
