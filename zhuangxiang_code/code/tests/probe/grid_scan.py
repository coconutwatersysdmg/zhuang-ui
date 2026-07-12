"""泛化探测网格扫描 + 结论。

用法（在 code/ 下）：
    python -m tests.probe.grid_scan            # 全网格
    python -m tests.probe.grid_scan --quick    # 小规模冒烟

覆盖维度：footprint 多样性、旋转敏感（baseline 固定朝向审计）、纯单层/混高、
大 mpm、suits 0.70 阈值跨越。每个场景在已知最优 N 下测 S/N。
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig  # noqa: E402
from src.packing.global_column_packer import GlobalColumnPacker  # noqa: E402

from . import generator as g  # noqa: E402
from .report import coverage_table, integrity_failures, worst_cells  # noqa: E402
from .runner import RunMetrics, run_order  # noqa: E402


@dataclass
class Scenario:
    order: g.Order
    configs: List[Dict] = field(default_factory=list)  # [{main_packer, allow_rotation, repeats}]
    note: str = ''


def _suits(boxes: List[Dict]) -> bool:
    """该订单整组是否被 GCP 判为「规则」（routes GCP），否则回退 baseline。"""
    gcp = GlobalColumnPacker(constraint_config=ConstraintConfig())
    return gcp.suits_group(boxes, g.TARGET)


def build_scenarios(quick: bool) -> List[Scenario]:
    """构造场景网格。quick=True 时缩小 N 与 repeats。"""
    gcp_rep = 1 if quick else 3
    n = (lambda full, q: q if quick else full)  # 规模选择

    scen: List[Scenario] = []

    # 1) 规则同构：基本 sanity（应 S/N=1.0）
    scen.append(Scenario(
        g.build_order('reg_multilayer', [(g.reg_multilayer(), n(3, 2))]),
        [{'main_packer': 'gcp', 'repeats': gcp_rep}],
        '规则多层 350×265×240（GCP 规范规则盘）'))
    scen.append(Scenario(
        g.build_order('reg_wide', [(g.reg_wide(), n(4, 2))]),
        [{'main_packer': 'gcp', 'repeats': gcp_rep}],
        '规则宽底 350×530×240'))

    # 2) footprint 多样性：一单内 3 种底面，每种 2 盘（同一 order_no → 同组）
    multi = g.build_order('multi_footprint', [
        (g.reg_multilayer(), 2), (g.reg_wide(), 2), (g.reg_big(), 2)])
    scen.append(Scenario(multi, [
        {'main_packer': 'gcp', 'repeats': gcp_rep},
        {'main_packer': 'beam', 'allow_rotation': True, 'repeats': 1},
    ], '3 种底面混在一单（GCP vs baseline）'))

    # 3) 旋转敏感审计（核心）：530×350 坏朝向，固定满盘 144<192、旋转 192
    rot = g.build_order('rotation_sensitive', [(g.rotation_sensitive(), n(4, 2))])
    scen.append(Scenario(rot, [
        {'main_packer': 'gcp', 'repeats': gcp_rep},                       # GCP 自带旋转
        {'main_packer': 'beam', 'allow_rotation': True, 'repeats': 1},    # baseline 开旋转
        {'main_packer': 'beam', 'allow_rotation': False, 'repeats': 1},   # baseline 固定朝向（审计）
    ], '★旋转敏感：固定朝向应装不满，旋转应救回'))

    # 4) 纯单层（C 盲区区）：350×265×480 只 1 层
    sing = g.build_order('single_layer', [(g.single_layer(), n(3, 2))])
    scen.append(Scenario(sing, [
        {'main_packer': 'gcp', 'repeats': gcp_rep},
        {'main_packer': 'beam', 'allow_rotation': True, 'repeats': 1},
    ], '纯单层 480 高（单层水平混装盲区）'))

    # 5) 混合高度：240 多层 + 480 单层 同一单
    mixh = g.build_order('mixed_height', [
        (g.reg_multilayer(), 2), (g.single_layer(), 2)])
    scen.append(Scenario(mixh, [{'main_packer': 'gcp', 'repeats': gcp_rep}],
                         '混高：多层 + 单层'))

    # 6) 大 mpm 少箱：700×530×720 满高单箱独占柱，每盘 8 箱×mpm24
    bigm = g.build_order('big_mpm', [(g.big_mpm(), n(3, 2))])
    scen.append(Scenario(bigm, [{'main_packer': 'gcp', 'repeats': gcp_rep}],
                         '大 mpm 少箱（满高独柱）'))

    # 6b) 异质柱指数均衡分配（压测柱级 Set-Partitioning ILP）：mpm 半1半3
    bal = g.build_balanced_order('balanced_mpm', n(4, 2))
    scen.append(Scenario(bal, [
        {'main_packer': 'gcp', 'repeats': gcp_rep},
        {'main_packer': 'beam', 'allow_rotation': True, 'repeats': 1},
    ], '★异质柱指数：须均匀配盘，劣解=超盘+欠盘'))

    # 7) suits 0.70 阈值跨越：规则(好) + 大底低指数(不可达标) 不同配比
    #    N=好盘数；坏盘几何不可达标，仅测「不达标尽量装满」
    for tag, good, bad in [('straddle_8good', 4, 1), ('straddle_5050', 2, 2),
                           ('straddle_3good', 1, 2), ('straddle_2good', 1, 4)]:
        if quick and tag in ('straddle_3good', 'straddle_2good'):
            continue
        o = g.build_order(tag, [
            (g.reg_multilayer(), good), (g.big_low_unreachable(), bad)])
        scen.append(Scenario(o, [{'main_packer': 'gcp', 'repeats': gcp_rep}],
                             f'阈值跨越 好{good}:坏{bad}盘'))

    return scen


def _print_rotation_audit(results: List[RunMetrics]) -> None:
    """旋转敏感订单的 A/B 对照（GCP / baseline旋转 / baseline固定）。"""
    rs = [m for m in results if m.order_name == 'rotation_sensitive']
    if not rs:
        return
    print('\n' + '=' * 64)
    print('★ baseline 固定朝向审计（旋转敏感订单 530×350，固定满盘 144<192）')
    print('=' * 64)
    for m in rs:
        path = ('GCP（自带旋转）' if m.main_packer == 'gcp'
                else f'baseline 旋转{"开" if m.allow_rotation else "关(固定朝向)"}')
        verdict = '达标=最优' if m.n_optimal and m.s_over_n >= 0.999 else '未达最优'
        print(f'  {path:<26} S/N = {m.s_median}/{m.n_optimal}  → {verdict}')
    norot = next((m for m in rs if m.main_packer == 'beam'
                  and not m.allow_rotation), None)
    rot = next((m for m in rs if m.main_packer == 'beam' and m.allow_rotation), None)
    if norot and rot and norot.n_optimal:
        delta = rot.s_median - norot.s_median
        print(f'\n  结论：baseline 固定朝向少达标 {delta} 盘 / {norot.n_optimal}；'
              f'开旋转（现默认 allow_box_rotation_90=True）救回 {delta} 盘。')
        if norot.s_median < norot.n_optimal:
            print('  → 固定朝向确为缺陷；现默认已开旋转修复（本测即回归护栏）。')


def _print_threshold(results: List[RunMetrics], scen: List[Scenario]) -> None:
    """阈值跨越：打印各 straddle 订单的 suits 路由 + S/N + 坏盘填充。"""
    rows = [(s, _suits(s.order.boxes)) for s in scen
            if s.order.name.startswith('straddle')]
    if not rows:
        return
    print('\n' + '=' * 64)
    print('suits_group 0.70 阈值跨越（好盘=规则可达标，坏盘=大底低指数不可达标）')
    print('=' * 64)
    print(f"{'订单':<18}{'路由':<10}{'好盘 S/N':>10}{'坏盘填充':>10}{'坏盘指数比':>12}")
    for s, routes_gcp in rows:
        m = next((r for r in results if r.order_name == s.order.name), None)
        if not m:
            continue
        fill = '   -  ' if m.avg_failed_fill is None else f'{m.avg_failed_fill*100:.1f}%'
        ratio = '   -  ' if m.avg_failed_mpm_ratio is None else f'{m.avg_failed_mpm_ratio*100:.1f}%'
        print(f"{s.order.name:<18}{'GCP' if routes_gcp else 'baseline':<10}"
              f"{f'{m.s_median}/{m.n_optimal}':>10}{fill:>10}{ratio:>12}")


def main(quick: bool = False) -> List[RunMetrics]:
    scen = build_scenarios(quick)
    total = sum(len(s.configs) for s in scen)
    print(f'泛化探测套件：{len(scen)} 场景 / {total} 次运行'
          f'（{"quick" if quick else "full"}）。\n')
    results: List[RunMetrics] = []
    i = 0
    for s in scen:
        for cfg in s.configs:
            i += 1
            mp = cfg.get('main_packer', 'gcp')
            ar = cfg.get('allow_rotation', True)
            tag = f'{mp}{"/rot" if mp == "beam" and ar else ""}'
            tag += '/norot' if mp == 'beam' and not ar else ''
            print(f'[{i}/{total}] {s.order.name} ({tag}) '
                  f'N={s.order.n_optimal} 箱={len(s.order.boxes)} ...',
                  flush=True)
            m = run_order(s.order, main_packer=mp, allow_rotation=ar,
                          repeats=cfg.get('repeats', 1))
            flag = ('OK' if m.n_optimal and m.gate_ok and m.conserved
                    and m.s_over_n >= 0.999 else
                    ('达标 %d/%d' % (m.s_median, m.n_optimal)))
            print(f'      → S={m.s_median}/{m.n_optimal} {flag} '
                  f'{m.runtime_s:.1f}s {m.error or ""}', flush=True)
            results.append(m)

    print('\n' + '=' * 64 + '\n覆盖率地图\n' + '=' * 64)
    print(coverage_table(results))
    _print_rotation_audit(results)
    _print_threshold(results, scen)

    bad = worst_cells(results)
    print('\n' + '=' * 64 + '\n最差区间（S/N<0.95，可达标却没达标）\n' + '=' * 64)
    if not bad:
        print('  无：所有「应达标」场景均 S/N≥0.95。')
    for m in bad:
        print(f'  {m.order_name} [{m.main_packer}'
              f'{"/norot" if m.main_packer == "beam" and not m.allow_rotation else ""}]'
              f' S/N={m.s_median}/{m.n_optimal}={m.s_over_n:.2f}')

    integ = integrity_failures(results)
    print('\n守恒/门禁完整性：' + ('全部通过 ✓' if not integ
          else f'{len(integ)} 个样本异常 ✗'))
    for m in integ:
        print(f'  ✗ {m.order_name} [{m.main_packer}] '
              f'conserved={m.conserved} gate={m.gate_ok} err={m.error}')
    return results


if __name__ == '__main__':
    main(quick='--quick' in sys.argv[1:])
