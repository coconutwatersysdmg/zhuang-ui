"""混合特性大订单分流验证。

一张订单混入 规则/旋转敏感/超高密度/异质柱/不可达标杂箱，跑默认算法（gcp），
看它能否：① 按特性把箱子分到合适的装箱路径（GCP 规则子集 / baseline 杂箱）；
② 全局尽量多达标；③ 不达标的尽量装满。打印路由日志 + 按箱型的达标/填充分解。

用法：cd code && python -m tests.probe.scan_mixed_order
"""

import io
import sys
from collections import defaultdict
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

_ROUTING_MARKS = ('组内子聚类', '主算法：全局列式', '不适合列式', '回退 baseline',
                  'GCP 盘数远超')


def _dominant_type(pallet: Dict) -> str:
    cnt: Dict[str, int] = defaultdict(int)
    for b in pallet.get('packed_items', []):
        cnt[str(b.get('type'))] += 1
    return max(cnt, key=cnt.get) if cnt else '?'


def main() -> None:
    order = g.build_mixed_characteristics_order()
    print(f'混合特性大订单：{len(order.boxes)} 箱，单一订单号，可达标 N={order.n_optimal}'
          f'（+{order.n_unreachable} 份不可达标杂箱）')
    print('构成：' + '，'.join(f'{tag}×{n}盘' for tag, n in order.specs) + '\n')

    cfg = ConstraintConfig(main_packer='gcp')
    wf = build_workflow(constraint_config=cfg)
    wf._report_persister = NullReportPersister()
    buf = io.StringIO()
    with redirect_stdout(buf):
        report = wf.run_with_boxes(order.boxes)
    out = buf.getvalue()

    print('=== 路由决策（算法日志摘录）===')
    for line in out.splitlines():
        if any(m in line for m in _ROUTING_MARKS):
            print('  ' + line.strip())

    pallets = report['pallets']
    print('\n=== 各托盘结果（按主导箱型）===')
    by_type = defaultdict(lambda: {'succ': 0, 'fail': 0, 'fills': []})
    for p in pallets:
        t = _dominant_type(p)
        ok = p.get('mpm_status') == 'SUCCESS'
        by_type[t]['succ' if ok else 'fail'] += 1
        by_type[t]['fills'].append(float(p.get('fill_rate', 0) or 0))
    head = f"{'主导箱型':<20}{'达标盘':>7}{'未达标':>7}{'平均填充':>10}{'平均指数':>10}"
    print(head)
    print('-' * len(head))
    for t, d in sorted(by_type.items()):
        ps = [p for p in pallets if _dominant_type(p) == t]
        avg_fill = sum(d['fills']) / len(d['fills']) if d['fills'] else 0
        avg_mpm = sum(float(p.get('mpm_total', 0)) for p in ps) / len(ps)
        print(f"{t:<20}{d['succ']:>7}{d['fail']:>7}{avg_fill*100:>9.1f}%{avg_mpm:>10.0f}")

    ov = report['summary']['overall']
    in_ids = sorted(b['id'] for b in order.boxes)
    out_ids = sorted(b['id'] for p in pallets for b in p['packed_items'])
    print('\n=== 全局 ===')
    print(f"  总托盘={ov['total_pallets']}  达标={ov['success_pallets']}  "
          f"未达标={ov['failed_pallets']}  （可达标上界 N={order.n_optimal}）")
    print(f"  守恒={'Y' if in_ids == out_ids else 'N(丢箱!)'}  "
          f"耗时={report.get('total_runtime_seconds', 0):.1f}s")
    print('\n判读：规则/旋转/超高密度应各自达标；不可达标杂箱(700×530)应未达标但尽量装满；'
          '\n      异质柱(700×265)是已知弱项；守恒须 Y。看不同特性是否被分别合理处理。')


if __name__ == '__main__':
    main()
