"""探测结果汇总与覆盖率地图格式化。"""

from typing import List

from .runner import RunMetrics


def _fmt_pct(x) -> str:
    return '   -  ' if x is None else f'{x * 100:5.1f}%'


def _flag(m: RunMetrics) -> str:
    if not m.gate_ok:
        return 'GATE✗'
    if not m.conserved:
        return 'CONS✗'
    if m.error:
        return 'ERR'
    if m.n_optimal and m.s_over_n >= 0.999:
        return 'OK'
    if m.n_optimal and m.s_over_n >= 0.95:
        return '~'
    return 'LOW'


def coverage_table(results: List[RunMetrics]) -> str:
    """逐样本一行：达标 S/N、波动带、失败盘填充、守恒/门禁、耗时。"""
    lines = []
    head = (f"{'场景':<22}{'算法':<6}{'旋转':<5}{'S/N':>9}"
            f"{'(min~max)':>10}{'失败盘填充':>11}{'失败盘指数比':>13}"
            f"{'守恒':>5}{'门禁':>5}{'耗时s':>8}  标记")
    lines.append(head)
    lines.append('-' * len(head))
    for m in results:
        sn = (f'{m.s_median}/{m.n_optimal}'
              if m.n_optimal else f'-/{m.n_unreachable}u')
        band = (f'{m.s_min}~{m.s_max}'
                if m.s_values and m.s_min != m.s_max else '')
        rot = ('on' if m.allow_rotation else 'OFF') if m.main_packer == 'beam' else '-'
        lines.append(
            f"{m.order_name:<22}{m.main_packer:<6}{rot:<5}{sn:>9}{band:>10}"
            f"{_fmt_pct(m.avg_failed_fill):>11}{_fmt_pct(m.avg_failed_mpm_ratio):>13}"
            f"{('Y' if m.conserved else 'N'):>5}{('Y' if m.gate_ok else 'N'):>5}"
            f"{m.runtime_s:>8.1f}  {_flag(m)}"
        )
    return '\n'.join(lines)


def worst_cells(results: List[RunMetrics], thresh: float = 0.95) -> List[RunMetrics]:
    """S/N < thresh 的样本（可达标却没达标的差距区间），按 S/N 升序。"""
    bad = [m for m in results
           if m.n_optimal and m.gate_ok and m.conserved and m.s_over_n < thresh]
    return sorted(bad, key=lambda m: m.s_over_n)


def integrity_failures(results: List[RunMetrics]) -> List[RunMetrics]:
    """守恒/门禁/异常样本（说明算法有 bug 或样本作废）。"""
    return [m for m in results
            if (not m.gate_ok) or (not m.conserved) or m.error]
