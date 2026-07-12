"""互借修复(合并装满)集成验证：跑真实数据集，对比失败盘数量与填充率。"""

import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

_CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CODE))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from collections import defaultdict

from run_packing import build_workflow
from src.config.constants import DATA_DIR
from src.data import load_boxes
from src.main.report_persister import NullReportPersister


def run(name: str) -> None:
    path = DATA_DIR / name
    if not path.exists():
        print(f'跳过（不存在）: {name}')
        return
    with redirect_stdout(io.StringIO()):
        boxes = load_boxes(str(path))
    wf = build_workflow()
    wf._report_persister = NullReportPersister()
    t0 = time.time()
    buf = io.StringIO()
    with redirect_stdout(buf):
        report = wf.run_with_boxes(boxes)
    rt = time.time() - t0
    ov = report['summary']['overall']
    # 守恒（id 统一转 str，兼容混合类型 id 数据集）
    in_ids = sorted(str(b['id']) for b in boxes)
    out_ids = sorted(str(i['id']) for p in report['pallets']
                     for i in p['packed_items'])
    conserved = in_ids == out_ids
    print(f'== {name} ==')
    print(f"总盘 {ov['total_pallets']}  达标 {ov['success_pallets']}  "
          f"未达标 {ov['failed_pallets']}  守恒 {conserved}  用时 {rt:.0f}s")
    g = defaultdict(lambda: {'S': 0, 'F': 0, 'fills': []})
    for p in report['pallets']:
        k = (p.get('pallet_type'), p.get('sales_order_no'))
        if p.get('mpm_status') == 'SUCCESS':
            g[k]['S'] += 1
        else:
            g[k]['F'] += 1
            g[k]['fills'].append(float(p.get('fill_rate') or 0))
    for k, v in sorted(g.items()):
        fl = v['fills']
        extra = (f"fail_fill min/avg/max={min(fl):.2f}/"
                 f"{sum(fl)/len(fl):.2f}/{max(fl):.2f}" if fl else '无失败盘')
        print(f"  {k[0]}|{k[1]}: S={v['S']} F={v['F']} {extra}")
    for line in buf.getvalue().splitlines():
        if '互借' in line or '互换' in line or '耗时拆解' in line:
            print(f'  {line.strip()}')
    print()


if __name__ == '__main__':
    for name in sys.argv[1:] or [
        '668箱子数据集.xlsx',
        '多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx',
        'selected_5000_full_regular_10_0_chain.xlsx',
    ]:
        run(name)
