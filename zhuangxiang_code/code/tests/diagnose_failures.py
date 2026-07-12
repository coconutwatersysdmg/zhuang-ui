"""阶段二诊断：未达标托盘根因分类。

读取一份装箱方案 JSON，按 (pallet_type, sales_order_no) 分组，回答：
- 实际达标 vs 理论达标上限（total_mpm // target）的差距 —— 组合/分配可救空间。
- 典型整层上限 canonical_layer_best 是否 < target —— 几何硬不可达信号。
- 失败盘的指数缺口分布（near/mid/deep）、填充率、箱数 —— 救援可行性。
- 失败盘里"锁住"的指数总量能再凑出几个达标盘 —— 重分配上限。

用法: python code/tests/diagnose_failures.py [plan.json]   (默认 output/NEW_full_m1.json)
"""
import sys
import json
from collections import defaultdict


def _load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main(path):
    plan = _load(path)
    pallets = plan.get('pallets', [])
    summary = plan.get('summary', {})
    by_type = summary.get('by_pallet_type', {})

    print('=' * 70)
    print('方案文件:', path)
    overall = summary.get('overall', {})
    print('整体: 总盘=%s 达标=%s 未达标=%s 平均缺口=%.2f 最大缺口=%.2f'
          % (overall.get('total_pallets'), overall.get('success_pallets'),
             overall.get('failed_pallets'), overall.get('avg_mpm_gap', 0),
             overall.get('max_mpm_gap', 0)))
    print('=' * 70)

    # 按组聚合 pallets
    groups = defaultdict(list)
    for pal in pallets:
        key = '%s__%s' % (pal.get('pallet_type'), pal.get('sales_order_no'))
        groups[key].append(pal)

    total_theoretical = 0
    total_success = 0
    total_redistrib = 0
    for key, pals in groups.items():
        target = pals[0].get('mpm_target') or 192
        success = [p for p in pals if p.get('mpm_status') == 'SUCCESS']
        failed = [p for p in pals if p.get('mpm_status') == 'FAILED']

        diag = (by_type.get(key, {}) or {}).get('index_diagnostics', {}) or {}
        total_mpm = diag.get('total_mpm')
        theoretical = diag.get('theoretical_success_pallets')
        residual = diag.get('residual_mpm')
        canonical = (diag.get('canonical_layer_best') or {}).get('best_mpm')
        if total_mpm is None:
            total_mpm = sum(float(p.get('mpm_total', 0) or 0) for p in pals)
        if theoretical is None:
            theoretical = int(total_mpm // target)

        # 失败盘缺口分布
        gaps = sorted(float(p.get('mpm_gap', 0) or 0) for p in failed)
        near = [g for g in gaps if g <= 32]
        mid = [g for g in gaps if 32 < g <= 96]
        deep = [g for g in gaps if g > 96]
        fills = [float(p.get('fill_rate', 0) or 0) for p in failed]
        counts = [len(p.get('packed_items', [])) for p in failed]
        failed_mpm = sum(float(p.get('mpm_total', 0) or 0) for p in failed)
        redistrib = int(failed_mpm // target)  # 失败盘指数理论上还能凑出几个达标盘

        total_theoretical += theoretical
        total_success += len(success)
        total_redistrib += redistrib

        geo_flag = ''
        if canonical is not None:
            geo_flag = ('  [整层上限<目标→几何偏紧]' if float(canonical) + 1e-9 < float(target)
                        else '  [整层上限>=目标→几何可达]')

        print()
        print('组 %s  (target=%g)' % (key, target))
        print('  盘: 总=%d 达标=%d 失败=%d' % (len(pals), len(success), len(failed)))
        print('  指数: total_mpm=%.1f 理论达标上限=%d 剩余指数=%s canonical整层上限=%s%s'
              % (total_mpm, theoretical, ('%.1f' % residual) if residual is not None else 'NA',
                 ('%g' % canonical) if canonical is not None else 'NA', geo_flag))
        print('  达标差距(理论-实际)= %d 盘' % (theoretical - len(success)))
        if failed:
            print('  失败盘缺口: near(<=32)=%d  mid(32-96)=%d  deep(>96)=%d  | 缺口 min/mean/max=%.0f/%.1f/%.0f'
                  % (len(near), len(mid), len(deep), gaps[0], sum(gaps) / len(gaps), gaps[-1]))
            print('  失败盘填充率 mean=%.3f  箱数 mean=%.1f (min=%d max=%d)'
                  % (sum(fills) / len(fills), sum(counts) / len(counts), min(counts), max(counts)))
            print('  失败盘锁住指数=%.1f → 理论可再凑 %d 个达标盘(重分配上限)' % (failed_mpm, redistrib))

    print()
    print('=' * 70)
    print('汇总: 实际达标=%d  理论达标上限=%d  差距=%d 盘' % (total_success, total_theoretical, total_theoretical - total_success))
    print('     失败盘锁住指数理论可再凑 %d 个达标盘(若能完美重分配+几何装下)' % total_redistrib)
    print('=' * 70)
    print('判读指引:')
    print(' - 若各组 canonical>=目标 且 (理论-实际) 差距大 → 根因偏"分配/装箱/救援不力"，重分配与救援有空间。')
    print(' - 若 canonical<目标 → 该组几何偏紧，单盘整层难达目标，需混层/放宽工艺余量。')
    print(' - near 缺口多 → 小步补齐/互借易救；deep 多 → 需整组重建或重分配。')


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'output/NEW_full_m1.json'
    main(path)
