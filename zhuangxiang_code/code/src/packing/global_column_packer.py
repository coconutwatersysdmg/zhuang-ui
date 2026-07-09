"""全局列式装箱 + 柱级 Set-Partitioning 组合优化（主装箱算法）。

思路（两步降维 + 组合优化）：
1. 凑柱：同底面箱按高度凑成 ≤ 托盘高的"柱"（同底面垂直堆叠→支撑率天然
   1.0），把 3D 装箱降为"柱的 2D 底面布局"。高度任意（不假设 120 倍数），
   规则箱凑满、不规则箱凑次满，均成合法柱。
2. 柱级组合优化：柱按 (底面, 指数) 聚合成柱类型，枚举"几何可装 + 指数达标"
   的候选盘 pattern，用 OR-Tools Set-Partitioning ILP **最大化达标盘数**。
3. 落地：选中盘用 265-单元列网格定坐标（530=2×265 对齐、消碎片）；残料柱
   再尽量装满成盘（达标优先，不达标则尽量满）。允许 90° 旋转。

约束：支撑率（柱内同底面=1.0）、旋转（落地选朝向 + layered_oriented 标记）、
吸盘/间隙/重心/小箱在下/同尺寸重箱在下 由整盘门禁逐盘复核。
不规则底面/高度的柱按 ceil 单元安全占格（不重叠），几何不佳时由门禁兜底。

无 OR-Tools 时自动回退为贪心装盘（仍正确，达标率略降）。
"""

import itertools
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..config.constants import PALLET_INDEX_TARGETS
from ..geometry.constraint_validator import validate_pallet_constraints
from .beam_search_packer import BeamSearchPacker
from .layered_packer import _assemble, _ffd_columns

try:
    from ortools.sat.python import cp_model
    _HAS_ORTOOLS = True
except ImportError:
    _HAS_ORTOOLS = False

_UNIT = 265.0  # y 向基本单元（mm）；规则底面 ylen ∈ {265, 530=2×265}
_OVERFLOW = 12.0  # 达标盘允许的指数溢出上限（控制候选规模）
_PATTERN_TYPE_CAP = 40  # 单类型在一盘内的枚举上限
_ILP_MAX_TYPES = 14  # 柱类型数 ≤ 此值才考虑精确 ILP（否则贪心，避免组合爆炸）
_MAX_ENUM = 2_000_000  # itertools.product 枚举空间上限（超过即改走贪心）
_ILP_TIME = 15.0  # 单组 ILP 时间上限（秒）
_CPSAT_TIME = 15.0  # 单盘 CP-SAT 精确摆柱时间上限（秒）


def _fp_orient(fp: Tuple[int, int]) -> Tuple[float, float]:
    """底面 (短,长) → (沿x, 沿y)：350/700 公共边沿 x，使列宽统一。"""
    a, b = fp
    if b == 700:
        return 700.0, float(a)
    if a == 350:
        return 350.0, float(b)
    if b == 350:
        return 350.0, float(a)
    return float(a), float(b)


def _orient_per(xl: float, yl: float, pallet_dims: Dict[str, float], tol: float) -> int:
    """单一底面满盘根数（floor 网格，取两朝向较优）。

    用于 suits_group 的真实 per 估算，替代 _grid_pack 的 350/265 量化估算——
    后者对非模数箱型严重低估（如 430×280：量化 16 vs 真实 25），会让本可旋转
    达标的非模数订单被误判"不适合"而回退固定朝向 baseline（L2 缺陷）。
    模数箱型两者相等，故对 668/5000 零回归。
    """
    pl = float(pallet_dims.get('length', 0) or 0)
    pw = float(pallet_dims.get('width', 0) or 0)
    if xl <= 0 or yl <= 0:
        return 0
    p1 = int(pl // (xl + tol)) * int(pw // (yl + tol))
    p2 = int(pl // (yl + tol)) * int(pw // (xl + tol))
    return max(p1, p2)


def _build_columns(boxes: List[Dict], pallet_dims: Dict[str, float]) -> List[Dict]:
    """按底面分组凑柱。返回柱列表 [{fp, xlen, ylen, boxes, idx}]。"""
    cap = float(pallet_dims.get('height', 0) or 0)
    by_fp: Dict[Tuple[int, int], List[Dict]] = defaultdict(list)
    for box in boxes:
        key = tuple(sorted((
            int(round(float(box.get('length', 0) or 0))),
            int(round(float(box.get('width', 0) or 0))),
        )))
        by_fp[key].append(box)
    cols: List[Dict] = []
    for fp, group in by_fp.items():
        xlen, ylen = _fp_orient(fp)
        for cb in _ffd_columns(group, cap):
            idx = sum(float(b.get('min_pack_multiple', 0) or 0) for b in cb)
            cols.append({'fp': fp, 'xlen': xlen, 'ylen': ylen, 'boxes': cb, 'idx': round(idx, 3)})
    return cols


def _col_units(ylen: float, tol: float) -> int:
    """柱沿 y 占用的 265-单元数（ceil，保证任意 ylen 不重叠）。"""
    return max(1, int(-(-(ylen + tol) // (_UNIT + tol))))


def _grid_pack(cols: List[Dict], pallet_dims: Dict[str, float], tol: float) -> Tuple[List[tuple], List[Dict]]:
    """265-单元列网格摆柱：700 跨 2 列、530 占 2 单元、放最空列。
    返回 (placed=[(col,x,y)], unplaced)。"""
    pall = float(pallet_dims.get('length', 0) or 0)
    palw = float(pallet_dims.get('width', 0) or 0)
    colstep = 350.0 + tol
    ncols = int(pall // colstep)
    if ncols < 1:
        return [], list(cols)
    unit_h = _UNIT + tol
    cap = int(palw // unit_h)
    xs = [c * colstep for c in range(ncols)]
    col_u = [0] * ncols
    placed, unplaced = [], []

    for c in sorted([c for c in cols if c['xlen'] > colstep], key=lambda c: -_col_units(c['ylen'], tol)):
        u = _col_units(c['ylen'], tol)
        span = int(-(-(c['xlen'] + tol) // colstep))
        best = None
        for s in range(ncols - span + 1):
            base = max(col_u[s:s + span])
            if base + u <= cap and (best is None or base < best[1]):
                best = (s, base)
        if best is None:
            unplaced.append(c)
            continue
        s, base = best
        placed.append((c, xs[s], base * unit_h))
        for k in range(s, s + span):
            col_u[k] = base + u
    for c in sorted([c for c in cols if c['xlen'] <= colstep], key=lambda c: -_col_units(c['ylen'], tol)):
        u = _col_units(c['ylen'], tol)
        cand = [(col_u[i], i) for i in range(ncols) if col_u[i] + u <= cap]
        if not cand:
            unplaced.append(c)
            continue
        _, i = min(cand)
        placed.append((c, xs[i], col_u[i] * unit_h))
        col_u[i] += u
    return placed, unplaced


def _center_placed(placed: List[tuple], pallet_dims: Dict[str, float], tol: float) -> List[tuple]:
    """把摆好的柱团整体平移到托盘中心，改善重心与边缘间隙（残料盘用）。"""
    if not placed:
        return placed
    pall = float(pallet_dims.get('length', 0) or 0)
    palw = float(pallet_dims.get('width', 0) or 0)
    x_min = min(x for _c, x, _y in placed)
    x_max = max(x + float(_c['xlen']) + tol for _c, x, _y in placed)
    y_min = min(y for _c, _x, y in placed)
    y_max = max(y + float(_c['ylen']) + tol for _c, _x, y in placed)
    dx = (pall - (x_max - x_min)) / 2.0 - x_min
    dy = (palw - (y_max - y_min)) / 2.0 - y_min
    dx = max(0.0, dx)
    dy = max(0.0, dy)
    return [(c, x + dx, y + dy) for c, x, y in placed]


def _cpsat_pack_2d(cols: List[Dict], pallet_dims: Dict[str, float],
                   time_limit: float = 8.0) -> Tuple[List[tuple], List[Dict]]:
    """CP-SAT 2D 精确摆柱（允许 90° 旋转），替代 265 固定网格的落地。

    坐标 ÷5 无损缩放（柱底面与托盘边长均为 5 的倍数）。目标：最大化装入指数
    （present 全装即达上界 OPT，找到满解即返回）。用 265 网格的部分解作
    warm-start（网格通常仅差一两根），稳定快速求解、消除多线程随机波动。无缝
    由门禁"达标盘免 gap"保证；落地后整体居中改善重心。
    返回 (placed=[(col, x, y)], unplaced)，坐标真实 mm。旋转柱返回 xlen/ylen
    互换后的浅拷贝（_assemble 据此按旋转朝向摆箱）。无 OR-Tools 回退网格。
    """
    if not cols:
        return [], []
    if not _HAS_ORTOOLS:
        return _grid_pack(cols, pallet_dims, 2.0)
    s = 5
    pw = int(round(float(pallet_dims.get('length', 0) or 0) / s))
    ph = int(round(float(pallet_dims.get('width', 0) or 0) / s))
    if pw < 1 or ph < 1:
        return [], list(cols)
    maxd = max(pw, ph)
    # 265 网格部分解作 warm-start hint：网格通常能装绝大多数柱（仅差一两根），
    # 以此为起点消除 CP-SAT 多线程在临界密度下的随机波动，稳定快速装满。
    grid_xy = {}
    _gp, _gu = _grid_pack(cols, pallet_dims, 2.0)
    for _gc, _gx, _gy in _gp:
        grid_xy[id(_gc)] = (_gx, _gy)
    m = cp_model.CpModel()
    pres, xs, ys, rots, weights = [], [], [], [], []
    xivs, yivs = [], []
    unplaced: List[Dict] = []
    model_cols: List[Dict] = []
    for c in cols:
        w0 = int(round(float(c['xlen']) / s))
        h0 = int(round(float(c['ylen']) / s))
        fit0 = (w0 <= pw and h0 <= ph)
        fit1 = (h0 <= pw and w0 <= ph)
        if not (fit0 or fit1):
            unplaced.append(c)  # 任何朝向都放不下托盘
            continue
        i = len(model_cols)
        model_cols.append(c)
        p = m.NewBoolVar(f'p{i}')
        if w0 != h0 and fit0 and fit1:  # 两朝向都可放 → 引入旋转变量
            r = m.NewBoolVar(f'r{i}')
            wi = m.NewIntVar(min(w0, h0), max(w0, h0), f'w{i}')
            hi = m.NewIntVar(min(w0, h0), max(w0, h0), f'h{i}')
            m.Add(wi == w0).OnlyEnforceIf(r.Not())
            m.Add(wi == h0).OnlyEnforceIf(r)
            m.Add(hi == h0).OnlyEnforceIf(r.Not())
            m.Add(hi == w0).OnlyEnforceIf(r)
        elif fit0:
            r, wi, hi = None, w0, h0
        else:  # 仅旋转朝向可放
            r, wi, hi = None, h0, w0
        x = m.NewIntVar(0, pw, f'x{i}')
        y = m.NewIntVar(0, ph, f'y{i}')
        xe = m.NewIntVar(0, pw + maxd, f'xe{i}')
        ye = m.NewIntVar(0, ph + maxd, f'ye{i}')
        m.Add(xe == x + wi)
        m.Add(ye == y + hi)
        m.Add(xe <= pw).OnlyEnforceIf(p)  # 仅装入的柱需在界内
        m.Add(ye <= ph).OnlyEnforceIf(p)
        xivs.append(m.NewOptionalIntervalVar(x, wi, xe, p, f'xi{i}'))
        yivs.append(m.NewOptionalIntervalVar(y, hi, ye, p, f'yi{i}'))
        pres.append(p)
        xs.append(x)
        ys.append(y)
        rots.append(r)
        weights.append(max(1, int(round(float(c.get('idx', 0) or 0) * 100))))
        gxy = grid_xy.get(id(c))
        if gxy is not None:  # warm-start：网格放好的柱作为初始解提示（present=1、不旋转）
            m.AddHint(p, 1)
            m.AddHint(x, max(0, min(pw, int(round(gxy[0] / s)))))
            m.AddHint(y, max(0, min(ph, int(round(gxy[1] / s)))))
            if r is not None:
                m.AddHint(r, 0)
    if not model_cols:
        return [], unplaced
    m.AddNoOverlap2D(xivs, yivs)
    # 目标：最大化装入指数。达标盘免 gap，不需要密铺次目标；且 present 全装即达
    # 指数上界（OPT），CP-SAT 找到满解即可立即返回——比带位置次目标快且稳得多
    # （后者要证明全局最优，对 95% 密度很慢、时快时慢）。落地后整体居中改善重心。
    m.Maximize(sum(pres[i] * weights[i] for i in range(len(model_cols))))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42  # 固定种子（注：多线程+时限仍有微小波动，见已知限制）
    st = solver.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        unplaced.extend(model_cols)  # 求解失败 → 全退残料
        return [], unplaced
    placed: List[tuple] = []
    for i, c in enumerate(model_cols):
        if solver.Value(pres[i]):
            col2 = dict(c)
            col2['_src'] = c  # 保留原柱引用：pack_group 据此标记"已用柱"，防重复装箱
            if rots[i] is not None and solver.Value(rots[i]) == 1:
                col2['xlen'], col2['ylen'] = c['ylen'], c['xlen']
            placed.append((col2, float(solver.Value(xs[i]) * s),
                           float(solver.Value(ys[i]) * s)))
        else:
            unplaced.append(c)
    return placed, unplaced


def _enumerate_patterns(types, counts, target, pallet_dims, tol):
    """枚举"指数达标 + 面积可行"的候选盘 pattern（柱类型计数向量）。

    几何用**面积必要条件**（柱底面积和 ≤ 盘面积）筛，不要求 265 网格能整齐
    摆下——真正的摆放交给落地阶段（先网格、装不下用 CP-SAT 精确摆柱，允许
    旋转/混合列宽）。这样不漏"面积可行但网格量化损失差几根"的达标组合
    （如 93% 填充的混合底面订单）。仅在柱类型少时调用，规模可控、拿全局最优。
    """
    cap_area = (float(pallet_dims.get('length', 0) or 0)
                * float(pallet_dims.get('width', 0) or 0))
    fp_area = []  # 各类型柱底面积
    per_cap = []  # 各类型一盘内用量上界 = 该底面满盘根数（几何上界）
    for t in types:
        xl, yl = _fp_orient(t[0])
        fp_area.append(xl * yl)
        per_cap.append(max(1, _orient_per(xl, yl, pallet_dims, tol)))
    # 单类型一盘内最多放 per_cap 根同底面柱（放不下更多）；用它替代固定
    # _PATTERN_TYPE_CAP=40 收紧枚举空间，让更多组走精确 ILP，且不漏解
    # （超过 per_cap 的组合几何上不可行，面积约束本就会剪掉）。
    ranges = [range(0, min(counts[i], per_cap[i]) + 1) for i in range(len(types))]
    patterns = []
    for combo in itertools.product(*ranges):
        if sum(combo) == 0:
            continue
        idx = sum(combo[i] * types[i][1] for i in range(len(types)))
        if idx < target - 1e-9 or idx > target + _OVERFLOW + 1e-9:
            continue
        area = sum(combo[i] * fp_area[i] for i in range(len(types)))
        if area <= cap_area + 1e-6:
            patterns.append(combo)
    return patterns


def _solve_ilp(patterns, counts, time_limit=20.0):
    """Set-Partitioning：max 达标盘数，s.t. 每柱类型用量 ≤ 库存。返回每 pattern 用量。"""
    m = cp_model.CpModel()
    x = [m.NewIntVar(0, max(counts) if counts else 0, f'x{p}') for p in range(len(patterns))]
    for i in range(len(counts)):
        m.Add(sum(patterns[p][i] * x[p] for p in range(len(patterns))) <= counts[i])
    m.Maximize(sum(x))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    solver.Solve(m)
    return [int(round(solver.Value(x[p]))) for p in range(len(patterns))]


def _same_type_boards(pools, target, pallet_dims, tol):
    """同类满盘：每个柱类型尽量铺满达标盘（无损主力）。就地消耗 pools，返回 [placed]。"""
    boards = []
    for t in list(pools.keys()):
        cl = pools[t]
        if not cl or t[1] <= 0:
            continue
        xl, yl = _fp_orient(t[0])
        placed_full, _ = _grid_pack([{'xlen': xl, 'ylen': yl}] * 60, pallet_dims, tol)
        per = len(placed_full)
        if per <= 0:
            continue
        need = max(1, int(-(-(target) // t[1])))  # ceil(target/idx) 根达标
        if need > per:
            continue  # 一盘铺满都不够达标 → 留给混合
        while len(cl) >= need:
            take = cl[:need]
            del cl[:need]
            placed, _u = _grid_pack(take, pallet_dims, tol)
            boards.append(placed)
    return boards


def _greedy_mixed_boards(cols, target, pallet_dims, tol):
    """贪心混合装盘：逐盘把柱塞到网格装满（最大化填充→最大化达标），收口。
    每盘优先放"放进去仍能装下、且推高指数最多"的柱；网格放满即收口。
    返回 (boards=[placed], leftover_cols)。"""
    remaining = list(cols)
    boards = []
    while remaining:
        plate = []
        # 反复挑一根"能装下"的柱加入，直到没有柱能再放进本盘
        progressed = True
        while progressed:
            progressed = False
            # 指数大的优先（快到 target），其次底面大的（占满空间）
            for c in sorted(remaining, key=lambda c: (-c['idx'], -(c['xlen'] * c['ylen']))):
                _, unplaced = _grid_pack(plate + [c], pallet_dims, tol)
                if not unplaced:
                    plate.append(c)
                    remaining.remove(c)
                    progressed = True
                    break
        if not plate:
            break
        boards.append(_grid_pack(plate, pallet_dims, tol)[0])
    return boards, remaining


class GlobalColumnPacker:
    """全局列式装箱 + 柱级组合优化主装箱器。pack_group 兼容现有契约。"""

    def __init__(self, constraint_config=None):
        if constraint_config is None:
            from ..config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._targets = dict(PALLET_INDEX_TARGETS)
        self._cols_cache = (None, None)  # (id(boxes), cols)：suits_group 与 pack_group 复用

    def _new_board(self, pallet_type, sales_order_no, seq, placed, packer, pallet_dims, target, gap=None):
        """组装一个盘并跑整盘门禁。门禁不过返回 None（调用方把柱退回残料 beam 兜底）。

        gap：柱间落地容差。CP-SAT 精确摆柱传 0（柱已紧贴无重叠）；265 网格摆柱
        传 None（默认 size_tolerance，柱间留缝）。门禁带 target，达标盘免 gap 校验
        （剩余空隙是高密度装载的几何必然，非偷懒）。
        """
        items = _assemble(placed, packer, pallet_dims, gap=gap)
        gate = validate_pallet_constraints(
            {'packed_items': items}, pallet_dims, constraint_config=self._cfg,
            target_mpm=target)
        if not gate.get('is_valid'):
            return None
        total = sum(float(b.get('min_pack_multiple', 0) or 0) for b in items)
        status = 'SUCCESS' if (target is not None and total + 1e-9 >= target) else 'FAILED'
        return {
            'pallet_id': f'{pallet_type}-{sales_order_no}-{seq}',
            'pallet_type': pallet_type,
            'sales_order_no': sales_order_no,
            'packed_items': items,
            'mpm_total': total,
            'mpm_target': target,
            'mpm_gap': (target - total) if target is not None else None,
            'mpm_status': status,
            'stability_checks': {'status': 'SUCCESS'},
        }

    def suits_group(self, boxes_in_group, target_mpm) -> bool:
        """判断该分组是否适合 GCP（规则数据）。不适合则由调用方回退旧算法。

        判据：凑柱后，能"单类满盘达标"或"与同类拼盘易达标"的柱占绝大多数。
        若大量柱是"非满柱 + 大底面"（满盘都到不了 target，柱化注定不达标），
        说明是不规则数据，GCP 会退步，应回退 baseline。
        """
        if target_mpm is None or not boxes_in_group:
            return False
        pallet_dims = boxes_in_group[0]['pallet_dims']
        cols = _build_columns(boxes_in_group, pallet_dims)
        self._cols_cache = (id(boxes_in_group), cols)  # 供随后的 pack_group 复用，免重复凑柱
        if not cols:
            return False
        target = float(target_mpm)
        # 每种底面"满盘指数上界"是否够 target（够 → 该底面的柱有达标希望）
        cap_by_fp = {}
        good_idx = 0.0
        total_idx = 0.0
        for c in cols:
            total_idx += c['idx']
            fp = c['fp']
            if fp not in cap_by_fp:
                xl, yl = _fp_orient(fp)
                # 真实几何 per（取两朝向较优），替代 _grid_pack 的 350/265 量化估算。
                # 满盘最高指数 = 每盘根数 × 该底面满柱指数（用本柱指数近似上界）
                cap_by_fp[fp] = _orient_per(xl, yl, pallet_dims, 2.0)
            # 该柱所在底面，满盘装 per 根同指数柱能否达标
            if cap_by_fp[fp] * c['idx'] >= target - 1e-9:
                good_idx += c['idx']
        # 有达标希望的柱指数占比 ≥ 70% 才用 GCP
        return total_idx > 0 and good_idx / total_idx >= 0.70

    def partition_suitable(self, boxes_in_group, target_mpm):
        """把一个销售订单组按底面切成 (regular, rest)。

        regular = 满盘能达标的底面的箱（走 GCP 精确 ILP）；rest = 其余底面的箱
        （走 baseline）。用于 workflow「组内子聚类」：当一张大订单里混有规则
        可列式子集（如 668 那种）与杂箱时，把规则子集单独抽出走 ILP，避免整组
        因杂箱被拖累退化（suits_group=False 整组回退 / 柱类型超阈贪心降级）。

        判据与 suits_group 同源（逐底面：满盘根数 × 该底面最大满柱指数 ≥ target）。
        - 纯规则组 → 所有底面都达标 → (全部, [])，调用方不拆，走原 GCP（零回归）。
        - 纯杂组   → 无达标底面     → ([], 全部)，调用方不拆，走原 baseline（零回归）。
        - 混合组   → (规则子集, 杂箱)，调用方拆成两个子组分别处理。
        """
        if target_mpm is None or not boxes_in_group:
            return list(boxes_in_group), []
        pallet_dims = boxes_in_group[0]['pallet_dims']
        cols = _build_columns(boxes_in_group, pallet_dims)
        if not cols:
            return [], list(boxes_in_group)
        target = float(target_mpm)
        cap_by_fp: Dict[Tuple[int, int], int] = {}
        max_idx_by_fp: Dict[Tuple[int, int], float] = defaultdict(float)
        for c in cols:
            fp = c['fp']
            if fp not in cap_by_fp:
                xl, yl = _fp_orient(fp)
                cap_by_fp[fp] = _orient_per(xl, yl, pallet_dims, 2.0)
            if c['idx'] > max_idx_by_fp[fp]:
                max_idx_by_fp[fp] = c['idx']
        good_fp = {
            fp for fp in cap_by_fp
            if cap_by_fp[fp] * max_idx_by_fp[fp] >= target - 1e-9
        }

        def _fp_of(b):
            return tuple(sorted((
                int(round(float(b.get('length', 0) or 0))),
                int(round(float(b.get('width', 0) or 0))),
            )))

        regular = [b for b in boxes_in_group if _fp_of(b) in good_fp]
        rest = [b for b in boxes_in_group if _fp_of(b) not in good_fp]
        return regular, rest

    def pack_group(self, pallet_type, sales_order_no, boxes_in_group, target_mpm):
        """对一个分组做全局列式装箱。返回 (type_plan, runtime, index_diag)。"""
        import time
        t0 = time.time()
        pallet_dims = boxes_in_group[0]['pallet_dims']
        packer = BeamSearchPacker(pallet_dims=pallet_dims, constraint_config=self._cfg)
        tol = packer.size_tolerance

        # 复用 suits_group 刚凑好的柱，避免重复凑柱
        if self._cols_cache[0] == id(boxes_in_group) and self._cols_cache[1] is not None:
            cols = self._cols_cache[1]
        else:
            cols = _build_columns(boxes_in_group, pallet_dims)
        plan: List[Dict] = []
        seq = 1
        boards: List[tuple] = []  # [(placed, gap)]：gap=0=CP-SAT 紧贴落地，None=265 网格

        if target_mpm is not None and cols:
            target = float(target_mpm)
            pools: Dict[tuple, List[Dict]] = defaultdict(list)
            for c in cols:
                pools[(c['fp'], c['idx'])].append(c)

            # 1) 小规模 → 精确 ILP（柱类型少 + 枚举空间可控，拿全局最优）
            types = sorted(pools.keys())
            counts = [len(pools[t]) for t in types]
            # 单类型一盘内用量上界 = 该底面满盘根数（per_layer，几何上界），
            # 远小于固定 40 → 枚举空间预判贴近真实，更多组走精确 ILP（B 轻量版）。
            per_caps = [max(1, _orient_per(*_fp_orient(t[0]), pallet_dims, tol))
                        for t in types]
            prod_scale = 1
            for c, pc in zip(counts, per_caps):
                prod_scale *= min(c, pc) + 1
                if prod_scale > _MAX_ENUM:
                    break
            use_ilp = _HAS_ORTOOLS and len(types) <= _ILP_MAX_TYPES and prod_scale <= _MAX_ENUM
            if use_ilp:
                patterns = _enumerate_patterns(types, counts, target, pallet_dims, tol)
                if patterns:
                    usage = _solve_ilp(patterns, counts, time_limit=_ILP_TIME)
                    pool_idx = {t: 0 for t in types}
                    for p, v in enumerate(usage):
                        for _ in range(v):
                            plate = []
                            for i, t in enumerate(types):
                                for _k in range(patterns[p][i]):
                                    plate.append(pools[t][pool_idx[t]])
                                    pool_idx[t] += 1
                            # 先试 265 网格（快、无缝）；网格量化损失装不下时用
                            # CP-SAT 精确摆柱（允许旋转/混合列宽，多装；达标盘免 gap）。
                            placed, unpl = _grid_pack(plate, pallet_dims, tol)
                            if unpl:
                                placed, unpl = _cpsat_pack_2d(
                                    plate, pallet_dims, time_limit=_CPSAT_TIME)
                                if placed:
                                    placed = _center_placed(placed, pallet_dims, tol)
                                    boards.append((placed, 0.0))
                            elif placed:
                                boards.append((placed, None))
            else:
                # 2) 大组 → 同类满盘（无损）+ 贪心混合（快、鲁棒）
                for placed in _same_type_boards(pools, target, pallet_dims, tol):
                    boards.append((placed, None))
                rest = [c for cl in pools.values() for c in cl]
                mixed, _rest = _greedy_mixed_boards(rest, target, pallet_dims, tol)
                for placed in mixed:
                    boards.append((placed, None))

            # 落地：每盘跑整盘门禁，过则进 plan、其柱计为已用；不过则其柱退回残料。
            used_ids = set()
            for placed, gap in boards:
                board = self._new_board(
                    pallet_type, sales_order_no, seq, placed, packer, pallet_dims,
                    target_mpm, gap=gap)
                if board is None:  # 门禁不过 → 柱退回残料，由 beam 兜底
                    continue
                plan.append(board)
                seq += 1
                # CP-SAT 落地的 col 是旋转浅拷贝，经 _src 找回原柱；网格落地即原柱
                used_ids |= {id(c.get('_src', c)) for c, _x, _y in placed}
            cols = [c for c in cols if id(c) not in used_ids]  # 残料柱（含门禁不过盘的柱）

        # 残料柱（或无 ILP 时全部柱）：拆回箱子交给 beam 装箱兜底。
        # beam 放置时逐箱校验全部约束（间隙/支撑/吸盘），保证残料盘必过门禁；
        # 达标优先、装不满则尽量满。半空柱造成的内部缝由 beam 自然避免。
        residual_boxes = [b for c in cols for b in c['boxes']]
        beam_dead = False  # beam 已无法装下任何残料 → 剩余全部单柱兜底，不再空跑 beam
        while residual_boxes:
            placed_items = []
            if not beam_dead:
                placed_items, _unfitted = packer.pack(
                    residual_boxes, target_mpm=target_mpm,
                    num_restarts=2, beam_width=4, candidate_limit=16,
                    stop_when_target_met=True, allow_skip_items=True,
                )
            if not placed_items:
                # beam 一个都装不下（极罕见）→ 取首箱单独成盘兜底，守恒优先（即便门禁
                # 不过也收下，绝不丢箱）。须经 _assemble 写 position/raw_*/吸盘，否则
                # output_formatter 取 position 会崩。剩余箱继续单柱兜底直到清空。
                beam_dead = True
                one = residual_boxes[0]
                col = {'xlen': float(one.get('length', 0) or 0),
                       'ylen': float(one.get('width', 0) or 0), 'boxes': [one]}
                items_one = _assemble([(col, 0.0, 0.0)], packer, pallet_dims)
                total_one = sum(float(b.get('min_pack_multiple', 0) or 0) for b in items_one)
                plan.append({
                    'pallet_id': f'{pallet_type}-{sales_order_no}-{seq}',
                    'pallet_type': pallet_type,
                    'sales_order_no': sales_order_no,
                    'packed_items': items_one,
                    'mpm_total': total_one,
                    'mpm_target': target_mpm,
                    'mpm_gap': (target_mpm - total_one) if target_mpm else None,
                    'mpm_status': 'FAILED',
                    'stability_checks': {'status': 'UNKNOWN'},
                })
                seq += 1
                residual_boxes = residual_boxes[1:]
                continue
            total = sum(float(b.get('min_pack_multiple', 0) or 0) for b in placed_items)
            status = 'SUCCESS' if (target_mpm is not None and total + 1e-9 >= target_mpm) else 'FAILED'
            plan.append({
                'pallet_id': f'{pallet_type}-{sales_order_no}-{seq}',
                'pallet_type': pallet_type,
                'sales_order_no': sales_order_no,
                'packed_items': placed_items,
                'mpm_total': total,
                'mpm_target': target_mpm,
                'mpm_gap': (target_mpm - total) if target_mpm is not None else None,
                'mpm_status': status,
                'stability_checks': {'status': 'SUCCESS'},
            })
            seq += 1
            placed_ids = {b['id'] for b in placed_items}
            residual_boxes = [b for b in residual_boxes if b['id'] not in placed_ids]

        success = sum(1 for b in plan if b['mpm_status'] == 'SUCCESS')
        index_diag = {
            'box_count': len(boxes_in_group),
            'total_mpm': sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes_in_group),
            'theoretical_success_pallets': int(
                sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes_in_group) // target_mpm
            ) if target_mpm else 0,
            'residual_mpm': 0,
            'global_column_packer': {'pallets': len(plan), 'success': success},
        }
        runtime = {'packing': time.time() - t0, 'topup': 0.0, 'retry': 0.0}
        return plan, runtime, index_diag
