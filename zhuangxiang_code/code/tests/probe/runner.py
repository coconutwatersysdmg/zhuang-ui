"""单订单跑真实装箱工作流并统计指标。

复用 run_packing.build_workflow / workflow.run_with_boxes（生产入口），
只通过 ConstraintConfig 切换主算法（gcp/beam）与 baseline 旋转开关
（allow_box_rotation_90），不改任何装箱原语。
"""

import io
import statistics
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_CODE_DIR = Path(__file__).resolve().parent.parent.parent  # .../code
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from run_packing import build_workflow  # noqa: E402
from src.config import ConstraintConfig  # noqa: E402
from src.main.report_persister import NullReportPersister  # noqa: E402

from .generator import Order  # noqa: E402


@dataclass
class RunMetrics:
    """单次（或多次取中位）运行结果。"""

    order_name: str
    main_packer: str
    allow_rotation: bool
    n_optimal: int
    n_unreachable: int
    s_values: List[int] = field(default_factory=list)  # 多次 S（达标盘数）
    total_pallets: int = 0
    failed_pallets: int = 0
    unknown_pallets: int = 0
    conserved: bool = True
    gate_ok: bool = True
    failed_fill_rates: List[float] = field(default_factory=list)
    failed_mpm_ratios: List[float] = field(default_factory=list)  # mpm_total/target
    runtime_s: float = 0.0
    error: Optional[str] = None

    @property
    def s_median(self) -> int:
        return int(statistics.median(self.s_values)) if self.s_values else 0

    @property
    def s_min(self) -> int:
        return min(self.s_values) if self.s_values else 0

    @property
    def s_max(self) -> int:
        return max(self.s_values) if self.s_values else 0

    @property
    def s_over_n(self) -> float:
        return self.s_median / self.n_optimal if self.n_optimal else float('nan')

    @property
    def avg_failed_fill(self) -> Optional[float]:
        return (sum(self.failed_fill_rates) / len(self.failed_fill_rates)
                if self.failed_fill_rates else None)

    @property
    def avg_failed_mpm_ratio(self) -> Optional[float]:
        return (sum(self.failed_mpm_ratios) / len(self.failed_mpm_ratios)
                if self.failed_mpm_ratios else None)


def _build_config(main_packer: str, allow_rotation: bool) -> ConstraintConfig:
    return ConstraintConfig(
        main_packer=main_packer, allow_box_rotation_90=allow_rotation)


def _run_once(boxes: List[Dict], cfg: ConstraintConfig) -> Optional[Dict]:
    """跑一次工作流，抑制 stdout；异常向上抛由调用方归类。"""
    workflow = build_workflow(constraint_config=cfg)
    workflow._report_persister = NullReportPersister()  # 不落地文件
    with redirect_stdout(io.StringIO()):
        return workflow.run_with_boxes(boxes)


def _collect(report: Dict, boxes: List[Dict], m: RunMetrics) -> None:
    """从一次 report 抽取指标，累加进 m。"""
    overall = report['summary']['overall']
    m.s_values.append(int(overall['success_pallets']))
    m.total_pallets = int(overall['total_pallets'])
    m.failed_pallets = int(overall['failed_pallets'])
    m.unknown_pallets = int(overall.get('unknown_pallets', 0))
    m.runtime_s = float(report.get('total_runtime_seconds', 0.0))

    # 守恒：输出箱 id 多重集 == 输入
    in_ids = sorted(b['id'] for b in boxes)
    out_ids = sorted(b['id'] for p in report['pallets']
                     for b in p['packed_items'])
    m.conserved = (in_ids == out_ids)

    # 失败盘（含 unknown 视为不达标）的填充率/指数比，测「不达标尽量装满」
    m.failed_fill_rates = []
    m.failed_mpm_ratios = []
    for p in report['pallets']:
        if p.get('mpm_status') == 'SUCCESS':
            continue
        fr = p.get('fill_rate')
        if fr is not None:
            m.failed_fill_rates.append(float(fr))
        tgt = p.get('mpm_target') or 0
        if tgt:
            m.failed_mpm_ratios.append(float(p.get('mpm_total', 0)) / float(tgt))


def run_order(order: Order, *, main_packer: str = 'gcp',
              allow_rotation: bool = True, repeats: int = 1) -> RunMetrics:
    """跑一个构造订单，返回指标（repeats>1 时取多次，S 报中位/极值）。"""
    cfg = _build_config(main_packer, allow_rotation)
    m = RunMetrics(
        order_name=order.name, main_packer=main_packer,
        allow_rotation=allow_rotation, n_optimal=order.n_optimal,
        n_unreachable=order.n_unreachable,
    )
    t0 = time.time()
    for _ in range(max(1, repeats)):
        try:
            report = _run_once(order.boxes, cfg)
        except ValueError as exc:          # 输出质量门禁违例 → 该样本作废
            m.gate_ok = False
            m.error = f'gate: {exc}'
            break
        except Exception as exc:            # noqa: BLE001 其它异常也记录，不中断网格
            m.error = f'{type(exc).__name__}: {exc}'
            break
        if report is None:
            m.error = 'report=None'
            break
        _collect(report, order.boxes, m)
    m.runtime_s = time.time() - t0
    return m
