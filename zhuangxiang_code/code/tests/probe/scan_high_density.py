"""超高密度压测：闭合「CP-SAT 装不满 → 爆盘回退」未测盲区。

构造体积填充 ≈98~99% 且可达标(每盘=192)的订单（单一密栅 + 混底面密栅），
跑 GCP，看：
1. S/N 是否仍 = N（高密度下还能不能达最优）；
2. 实际达成填充率；
3. 是否触发 GCP 爆盘回退（workflow._run_group_gcp 判 CP-SAT 装不满→回退 baseline）。

并把真实 668v2(约 93% 填充)一并跑作锚点。
用法：cd code && python -m tests.probe.scan_high_density
"""

import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig  # noqa: E402
from src.config.constants import DATA_DIR  # noqa: E402
from src.data import load_boxes  # noqa: E402
from src.main.report_persister import NullReportPersister  # noqa: E402
from run_packing import build_workflow  # noqa: E402

from . import generator as g  # noqa: E402

_RETREAT_MARKS = ('回退 baseline', 'CP-SAT 装不满', '盘数远超理论')


def _run_capture(boxes: List[Dict], main_packer: str = 'gcp') -> Tuple[Optional[Dict], str, float]:
    """跑工作流并捕获 stdout（用于检测爆盘回退），返回 (report, stdout, 秒)。"""
    cfg = ConstraintConfig(main_packer=main_packer)
    wf = build_workflow(constraint_config=cfg)
    wf._report_persister = NullReportPersister()
    buf = io.StringIO()
    t0 = time.time()
    with redirect_stdout(buf):
        report = wf.run_with_boxes(boxes)
    return report, buf.getvalue(), time.time() - t0


def _metrics(report: Dict, boxes: List[Dict], n_opt: Optional[int],
             stdout: str, dt: float) -> Dict:
    pallets = report['pallets']
    succ = [p for p in pallets if p.get('mpm_status') == 'SUCCESS']
    fills = [float(p.get('fill_rate', 0) or 0) for p in pallets]
    succ_fills = [float(p.get('fill_rate', 0) or 0) for p in succ]
    in_ids = sorted(b['id'] for b in boxes)
    out_ids = sorted(b['id'] for p in pallets for b in p['packed_items'])
    return {
        's': len(succ), 'n_opt': n_opt, 'total_pallets': len(pallets),
        'avg_succ_fill': sum(succ_fills) / len(succ_fills) if succ_fills else None,
        'max_fill': max(fills) if fills else None,
        'retreat': any(m in stdout for m in _RETREAT_MARKS),
        'conserved': in_ids == out_ids,
        'runtime': dt,
    }


def _fmt(x, pct=True) -> str:
    if x is None:
        return '  -  '
    return f'{x*100:5.1f}%' if pct else f'{x}'


def run_synthetic() -> None:
    print('超高密度构造订单（GCP）：达标率 / 实际填充 / 是否爆盘回退\n' + '=' * 78)
    cases = [
        ('dense_A 358×558 4×4', g.build_order(
            'dA', [(g.dense_grid_a(), 3)]), g.pallet_fill_rate(g.dense_grid_a())),
        ('dense_B 718×558 2×4', g.build_order(
            'dB', [(g.dense_grid_b(), 3)]), g.pallet_fill_rate(g.dense_grid_b())),
        ('dense_C 178×558 8×4', g.build_order(
            'dC', [(g.dense_grid_c(), 3)]), g.pallet_fill_rate(g.dense_grid_c())),
        ('dense_MIX 358+718', g.build_dense_mixed_order('dM', 3), 0.992),
    ]
    head = (f"{'场景':<24}{'理论填充':>9}{'S/N':>8}{'总盘':>6}"
            f"{'达成填充':>10}{'回退?':>7}{'守恒':>6}{'耗时s':>8}")
    print(head)
    print('-' * len(head))
    for name, order, theo_fill in cases:
        report, out, dt = _run_capture(order.boxes)
        m = _metrics(report, order.boxes, order.n_optimal, out, dt)
        sn = '%d/%d' % (m['s'], m['n_opt'])
        retreat = '是' if m['retreat'] else '否'
        cons = 'Y' if m['conserved'] else 'N'
        print(f"{name:<24}{_fmt(theo_fill):>9}{sn:>8}"
              f"{m['total_pallets']:>6}{_fmt(m['avg_succ_fill']):>10}"
              f"{retreat:>7}{cons:>6}{m['runtime']:>8.1f}")


def run_real_anchor() -> None:
    """真实 668v2(约 93% 填充)锚点：高填充真实数据 GCP 是否达标不崩。"""
    print('\n' + '=' * 78 + '\n真实高填充锚点 668箱子数据集2.xlsx（约 93% 填充）\n' + '=' * 78)
    p = DATA_DIR / '668箱子数据集2.xlsx'
    if not p.exists():
        print('  (文件不存在，跳过)')
        return
    with redirect_stdout(io.StringIO()):
        boxes = load_boxes(str(p))
    if not boxes:
        print('  (加载失败/空)')
        return
    report, out, dt = _run_capture(boxes)
    m = _metrics(report, boxes, None, out, dt)
    print(f"  箱数={len(boxes)} 总盘={m['total_pallets']} 达标={m['s']} "
          f"达标盘均填充={_fmt(m['avg_succ_fill'])} 最高填充={_fmt(m['max_fill'])} "
          f"回退={'是' if m['retreat'] else '否'} 守恒={'Y' if m['conserved'] else 'N'} "
          f"用时={m['runtime']:.1f}s")


def main() -> None:
    run_synthetic()
    run_real_anchor()
    print('\n' + '=' * 78)
    print('判读：S/N=N 且「回退=否」→ 高密度下 GCP 仍达最优、未触发爆盘回退，盲区闭合；'
          '\n      若「回退=是」但 S/N 仍=N → 回退 baseline 兜住、不退步（设计预期）；'
          '\n      若 S/N<N → 高密度确为残留盲区，需进一步处理。')


if __name__ == '__main__':
    main()
