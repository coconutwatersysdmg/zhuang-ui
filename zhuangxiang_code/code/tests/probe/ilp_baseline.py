"""全局 ILP 基线（阶段 A）：求「达标盘数」的有证明上界，对照算法实际达标数。

目的：回答「5000 箱的 141 是否已是最优」。方法是求松弛问题的精确最优——
去掉三维几何摆放，只保留三类**任何真实方案都必须满足**的约束：
  1. 指数约束：达标盘上箱子的 min_pack_multiple 之和 ≥ target（192）；
  2. 体积约束：一个托盘上箱子总体积 ≤ 托盘容积（几何摆放的必要条件）；
  3. 分组约束：同 (托盘类型, 销售订单号[, case_group]) 才能同托盘
     （与生产分组键完全一致，直接复用 OrderProcessor.group_by_order）。
放不进托盘的箱子（高>托盘高 或 底面超托盘+容差）从模型中剔除（合法收紧）。

结论解读（关键）：
  - 松弛最优 = 真实最优的**上界**（真实方案都满足松弛，故松弛最优 ≥ 真实最优）。
  - 若 上界 == 算法达标数 → **证明算法已最优**（在业务规则约束下）。
  - 若 上界 > 算法达标数 → 只能说差距 ≤ (上界-达标数)，仍无定论：缺口可能来自
    几何不可行（松弛太松），也可能来自算法未找到。需阶段 B（几何验证列）细分。
  - CP-SAT 未证明最优时，其 ObjectiveBound 仍是合法上界（松弛最优 ≤ bound）；
    此时报告 [incumbent, bound] 区间，取 bound 为有效上界。

用法（cd code 后）：
  python -m tests.probe.ilp_baseline                       # 默认 5000 箱数据集
  python -m tests.probe.ilp_baseline --boxes 1000          # 先试前 1000 箱
  python -m tests.probe.ilp_baseline --boxes 2000 --run-algo   # 同子集跑真实算法对比
  python -m tests.probe.ilp_baseline --run-algo --time-limit 1800
"""

import argparse
import io
import math
import sys
import time
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent.parent  # .../code
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from ortools.sat.python import cp_model  # noqa: E402

from src.config.constants import DATA_DIR, PALLET_INDEX_TARGETS  # noqa: E402
from src.data import load_boxes  # noqa: E402
from src.main.order_processor import OrderProcessor  # noqa: E402

_MPM_SCALE = 1000        # mpm 定点化（×1000 取整）
_VOL_DIV = 1000.0        # 体积 mm^3 → cm^3，压小系数
_XY_TOL = 2.0            # 与生产 xy_tolerance 一致：底面允许超托盘 2mm
_MAX_PALLET_SLOTS = 400  # 单组候选达标盘槽位上限（安全阀）


def _pallet_dims(boxes: List[Dict]) -> Tuple[float, float, float]:
    d = boxes[0].get('pallet_dims') or {}
    return (float(d.get('length', 0) or 0), float(d.get('width', 0) or 0),
            float(d.get('height', 0) or 0))


def _fits_pallet(box: Dict, pl: float, pw: float, ph: float) -> bool:
    """箱子能否单独放进托盘（允许 90° 旋转 + xy 容差）。放不进的剔除。"""
    h = float(box.get('height', 0) or 0)
    if h > ph + 1e-9:
        return False
    bl, bw = sorted((float(box.get('length', 0) or 0),
                     float(box.get('width', 0) or 0)))
    sl, sw = sorted((pl, pw))
    return bl <= sl + _XY_TOL + 1e-9 and bw <= sw + _XY_TOL + 1e-9


def solve_group_upper_bound(
    boxes: List[Dict], target: float, time_limit_s: float,
) -> Dict:
    """对一个分组求「达标盘数」松弛上界。

    模型：P 个候选达标盘槽位（P = floor(组总指数/target)），
    x[t,p] = 箱型 t 放入盘 p 的数量，y[p] = 盘 p 是否启用（计入达标数）。
    约束：指数 ≥ target·y[p]；体积 ≤ 托盘容积；库存 ≤ 存量；
    对称破除：y 递减、盘指数递减。目标：max Σy。

    Returns:
        dict: ub(有效上界)/incumbent/status/n_boxes/n_types/total_index/
              excluded(剔除的放不进箱数)/runtime_s。
    """
    pl, pw, ph = _pallet_dims(boxes)
    usable = [b for b in boxes if _fits_pallet(b, pl, pw, ph)]
    excluded = len(boxes) - len(usable)

    total_index = sum(float(b.get('min_pack_multiple', 0) or 0) for b in usable)
    p_cap = int(total_index // target) if target > 0 else 0
    result = {
        'n_boxes': len(boxes), 'excluded': excluded,
        'total_index': total_index, 'index_ub': p_cap,
        'n_types': 0, 'ub': 0, 'incumbent': 0,
        'status': 'TRIVIAL', 'runtime_s': 0.0,
    }
    if p_cap <= 0 or not usable:
        return result
    n_slots = min(p_cap, _MAX_PALLET_SLOTS)

    # 按 (mpm, 体积) 聚合为箱型——松弛只关心这两个量
    agg: Dict[Tuple[int, int], int] = defaultdict(int)
    for b in usable:
        mpm = int(round(float(b.get('min_pack_multiple', 0) or 0) * _MPM_SCALE))
        vol = int(round(
            float(b['length']) * float(b['width']) * float(b['height'])
            / _VOL_DIV))
        agg[(mpm, vol)] += 1
    types = sorted(agg.items())
    result['n_types'] = len(types)

    cap_vol = int(round(pl * pw * ph / _VOL_DIV))
    tgt = int(round(target * _MPM_SCALE))

    model = cp_model.CpModel()
    y = [model.NewBoolVar(f'y{p}') for p in range(n_slots)]
    x = {}
    for ti, ((mpm, vol), cnt) in enumerate(types):
        for p in range(n_slots):
            x[ti, p] = model.NewIntVar(0, cnt, f'x{ti}_{p}')
    for ti, ((mpm, vol), cnt) in enumerate(types):
        model.Add(sum(x[ti, p] for p in range(n_slots)) <= cnt)
    idx_of_p = []
    for p in range(n_slots):
        idx_p = sum(x[ti, p] * mpm for ti, ((mpm, _v), _c) in enumerate(types))
        vol_p = sum(x[ti, p] * vol for ti, ((_m, vol), _c) in enumerate(types))
        model.Add(idx_p >= tgt * y[p])
        model.Add(vol_p <= cap_vol)
        idx_of_p.append(idx_p)
    for p in range(n_slots - 1):        # 对称破除
        model.Add(y[p] >= y[p + 1])
        model.Add(idx_of_p[p] >= idx_of_p[p + 1])
    model.Maximize(sum(y))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 8
    t0 = time.time()
    status = solver.Solve(model)
    result['runtime_s'] = time.time() - t0

    name = solver.StatusName(status)
    result['status'] = name
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result['incumbent'] = int(round(solver.ObjectiveValue()))
        bound = int(math.floor(solver.BestObjectiveBound() + 1e-6))
        result['ub'] = (result['incumbent'] if status == cp_model.OPTIMAL
                        else min(bound, p_cap))
    else:  # 时限内无可行解：退回指数上界（仍合法，只是松）
        result['ub'] = p_cap
        result['incumbent'] = 0
    return result


def _run_algo_on(boxes: List[Dict]) -> Tuple[int, int, float]:
    """同一子集跑真实生产工作流，返回 (达标盘, 总盘, 用时s)。"""
    from run_packing import build_workflow
    from src.main.report_persister import NullReportPersister
    workflow = build_workflow()
    workflow._report_persister = NullReportPersister()
    t0 = time.time()
    with redirect_stdout(io.StringIO()):
        report = workflow.run_with_boxes(boxes)
    overall = report['summary']['overall']
    return (int(overall['success_pallets']), int(overall['total_pallets']),
            time.time() - t0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--data', default='多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx',
        help='数据集（相对 data/ 或绝对路径）')
    parser.add_argument('--boxes', type=int, default=0,
                        help='只取前 N 箱（0=全量）')
    parser.add_argument('--time-limit', type=float, default=600.0,
                        help='每组 CP-SAT 时限秒（默认 600）')
    parser.add_argument('--run-algo', action='store_true',
                        help='同子集跑真实算法对比达标数')
    args = parser.parse_args()

    path = Path(args.data)
    if not path.is_absolute():
        path = DATA_DIR / args.data
    print(f'数据集: {path}')
    with redirect_stdout(io.StringIO()):
        boxes = load_boxes(str(path))
    if not boxes:
        print('错误：数据加载失败/为空。')
        sys.exit(1)
    if args.boxes > 0:
        boxes = boxes[:args.boxes]
    print(f'箱数: {len(boxes)}（--boxes {args.boxes or "全量"}）\n')

    groups = OrderProcessor.group_by_order(boxes)
    print(f'分组数: {len(groups)}（键=生产分组：托盘类型×订单[×case_group]）')
    head = (f"{'组':<34}{'箱数':>6}{'剔除':>5}{'箱型':>5}{'总指数':>9}"
            f"{'指数UB':>7}{'松弛UB':>7}{'状态':>10}{'用时s':>8}")
    print(head)
    print('-' * len(head))

    total_ub = 0
    all_proved = True
    for (ptype, order), gb in sorted(groups.items()):
        target = PALLET_INDEX_TARGETS.get(ptype)
        if target is None:
            print(f'{ptype}|{order}: 无 target 配置，跳过（不计上界）')
            all_proved = False
            continue
        r = solve_group_upper_bound(gb, float(target), args.time_limit)
        total_ub += r['ub']
        proved = r['status'] == 'OPTIMAL' or r['ub'] == r['incumbent']
        all_proved = all_proved and (r['status'] in ('OPTIMAL', 'TRIVIAL'))
        key = f'{ptype}|{order}'[:34]
        print(f"{key:<34}{r['n_boxes']:>6}{r['excluded']:>5}"
              f"{r['n_types']:>5}{r['total_index']:>9.1f}{r['index_ub']:>7}"
              f"{r['ub']:>7}{r['status']:>10}{r['runtime_s']:>8.1f}")

    print('-' * len(head))
    proof = '（各组均证到最优，上界精确）' if all_proved else \
        '（有组未证到最优，上界取 CP-SAT bound，仍合法但可能偏松）'
    print(f'松弛上界合计（达标盘数 ≤）: {total_ub} {proof}')

    if args.run_algo:
        print('\n同子集跑真实算法（生产配置 main_packer=gcp）...')
        s, tot, rt = _run_algo_on(boxes)
        print(f'算法达标盘: {s} / 总盘 {tot}（{rt:.1f}s）')
        gap = total_ub - s
        if gap <= 0:
            print(f'★ 结论：达标数 {s} == 松弛上界 {total_ub} → '
                  f'**证明算法在该数据上已最优**（业务规则约束下）。')
        else:
            print(f'★ 结论：区间 [{s}, {total_ub}]，缺口 {gap} 盘。'
                  f'缺口来源未定（几何不可行 vs 算法未找到），'
                  f'需阶段 B（几何验证列）才能细分。')


if __name__ == '__main__':
    main()
