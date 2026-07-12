"""路由对比：高底面多样性的无异质柱大订单，拆分能否提高达标率？

机制：GCP 柱级 ILP 有类型上限（_ILP_MAX_TYPES=14），底面种类超过即退贪心。
若一张订单底面种类很多，联合 GCP 退贪心可能不达最优；把它拆成更细子订单
（每个底面种类少、各自走精确 ILP）也许能恢复最优。

对比四种路由（同一批箱、同一已知 N）：
  ① 联合 GCP（默认，整单一组）
  ② 全 baseline（main_packer=beam）
  ③ 二分拆（底面分两半 → 两个子订单，各自 ≤ 类型上限 → GCP 走 ILP）
  ④ 按底面全拆（每底面一个子订单 → 各自单一类型 → GCP 必走 ILP）

用法：cd code && python -m tests.probe.scan_routing_alternatives
"""

import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List

_CODE_DIR = Path(__file__).resolve().parent.parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig  # noqa: E402
from src.main.report_persister import NullReportPersister  # noqa: E402
from run_packing import build_workflow  # noqa: E402

from . import generator as g  # noqa: E402


def _run(boxes: List[Dict], main_packer: str = 'gcp') -> Dict:
    cfg = ConstraintConfig(main_packer=main_packer)
    wf = build_workflow(constraint_config=cfg)
    wf._report_persister = NullReportPersister()
    t0 = time.time()
    with redirect_stdout(io.StringIO()):
        rep = wf.run_with_boxes(boxes)
    ov = rep['summary']['overall']
    in_ids = sorted(b['id'] for b in boxes)
    out_ids = sorted(b['id'] for p in rep['pallets'] for b in p['packed_items'])
    return {'succ': ov['success_pallets'], 'total': ov['total_pallets'],
            'conserved': in_ids == out_ids, 'runtime': time.time() - t0}


def _retag(boxes: List[Dict], key) -> List[Dict]:
    """复制箱子并改 sales_order_no（按 key(box) 分子订单），不改原箱。"""
    return [{**b, 'sales_order_no': key(b)} for b in boxes]


def main() -> None:
    order = g.build_mixed_nohet_order()
    n_fp = len(order.specs)
    print(f'高多样性无异质柱订单：{len(order.boxes)} 箱、{n_fp} 种底面、'
          f'已知 N={order.n_optimal}（GCP 柱类型上限=14，{n_fp}>14 → 联合退贪心）\n')

    # 二分拆：底面 F00..F07 → 子订单 A，其余 → B（各 ≤ 14 类型）
    def half(b):
        idx = int(str(b['type'])[1:]) if str(b['type'])[1:].isdigit() else 0
        return f"{order.name}-A" if idx < n_fp // 2 else f"{order.name}-B"

    variants = [
        ('① 联合 GCP（默认）', order.boxes, 'gcp'),
        ('③ 二分拆 → GCP', _retag(order.boxes, half), 'gcp'),
        ('④ 按底面全拆 → GCP', _retag(order.boxes, lambda b: f"{order.name}-{b['type']}"), 'gcp'),
    ]
    if '--with-baseline' in sys.argv[1:]:  # 全 baseline 在 2000 箱上很慢，按需开
        variants.insert(1, ('② 全 baseline', order.boxes, 'beam'))
    head = f"{'路由':<24}{'达标 S/N':>12}{'总盘':>7}{'守恒':>6}{'耗时s':>9}"
    print(head, flush=True)
    print('-' * len(head), flush=True)
    rows = []
    for label, boxes, mp in variants:
        r = _run(boxes, mp)
        rows.append((label, r))
        sn = '%d/%d' % (r['succ'], order.n_optimal)
        print(f"{label:<24}{sn:>12}{r['total']:>7}"
              f"{('Y' if r['conserved'] else 'N'):>6}{r['runtime']:>9.1f}", flush=True)

    joint = rows[0][1]['succ']
    best = max(r['succ'] for _l, r in rows)
    print('\n=== 判读 ===')
    if best > joint:
        gain = best - joint
        winner = next(l for l, r in rows if r['succ'] == best)
        print(f'  拆分有效：联合 GCP {joint}/{order.n_optimal}，最佳「{winner}」'
              f'{best}/{order.n_optimal}（+{gain} 盘）。')
        print('  → 高底面多样性下联合 GCP 退贪心丢盘，按子订单拆分让各自走 ILP 可恢复。')
    else:
        print(f'  联合 GCP {joint}/{order.n_optimal} 已是最佳，拆分无增益'
              f'（达标上界 N={order.n_optimal}）。')


if __name__ == '__main__':
    main()
