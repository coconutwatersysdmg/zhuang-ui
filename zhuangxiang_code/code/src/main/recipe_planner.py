"""配方配额规划器：全局"指数×几何"联合分配，最大化达标托盘数量。

背景：主装箱是逐托盘贪心，会把稀缺的"伴层箱"过量烧进早期托盘
（实测组 PAIN25450MZ06S：9 个成功盘吃掉 304/305 个小伴层箱，留下
121 个 480 高大箱无伴层可配，全部失败）。本模块在装箱前先做全组
配额规划：

- 层   = 单一箱型的满格栅整层（per_x × per_y 个），层指数 = 格数 × mpm；
- 顶带 = 配方最顶部的整行组合（每行 = 某型沿 x 摆满 per_x 个），可混型，
         行高 ≤ 剩余高度、Σ行宽 ≤ 托盘宽；
- 配方 = 若干整层（自下而上）+ 至多一个顶带：Σ高 ≤ 托盘高，
         Σ指数 ≥ target 且尽量少溢出（允许"最后一跳"小幅超帽）；
- 分配 = 库存约束下最大化配方实例数（多启发式随机重启贪心）。

规划产物只是候选池：每个实例必须用真实装箱器实装、并通过
validate_pallet_constraints 整盘门禁后才允许采纳；本模块不做
支撑/吸盘/间隙的细节判定，几何真伪由装箱器与门禁裁决。
"""

from typing import Dict, List, Optional, Set, Tuple
import math
import random


def build_type_table(
    boxes: List[Dict],
    pallet_dims: Dict[str, float],
    xy_tolerance: float = 2.0,
) -> List[Dict]:
    """按 (L,W,H,mpm) 聚类并计算整层格栅参数。

    Returns:
        类型表，每项含 idx/key/length/width/height/mpm/count/
        per_x/per_y/per_layer/layer_mpm/boxes（按 id 排序的原箱列表）。
    """
    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    table: Dict[Tuple, Dict] = {}
    for box in boxes:
        key = (
            float(box.get('length', 0) or 0),
            float(box.get('width', 0) or 0),
            float(box.get('height', 0) or 0),
            float(box.get('min_pack_multiple', 0) or 0),
        )
        entry = table.get(key)
        if entry is None:
            length, width, height, mpm = key
            per_x = int(pallet_length // (length + xy_tolerance)) if length > 0 else 0
            per_y = int(pallet_width // (width + xy_tolerance)) if width > 0 else 0
            entry = {
                'key': key,
                'length': length,
                'width': width,
                'height': height,
                'mpm': mpm,
                'per_x': per_x,
                'per_y': per_y,
                'per_layer': per_x * per_y,
                'layer_mpm': per_x * per_y * mpm,
                'row_mpm': per_x * mpm,
                'row_width': width + xy_tolerance,
                'boxes': [],
            }
            table[key] = entry
        entry['boxes'].append(box)

    types = sorted(table.values(), key=lambda t: (-t['layer_mpm'], t['height'], t['key']))
    for idx, t in enumerate(types):
        t['idx'] = idx
        t['count'] = len(t['boxes'])
        t['boxes'].sort(key=lambda b: str(b.get('id')))
    return types


def _enumerate_top_bands(
    types: List[Dict],
    h_left: float,
    pallet_width: float,
    mpm_lo: float,
    mpm_hi: float,
    max_bands: int = 200,
) -> List[Tuple[Tuple[Tuple[int, int], ...], float]]:
    """枚举顶带候选：整行组合 (type_idx, n_rows)，Σ行宽<=托盘宽。

    Returns:
        [(band_sig, band_mpm)]；band_sig = ((type_idx, n_rows), ...)
    """
    rowable = [
        t for t in types
        if t['per_x'] > 0 and t['height'] <= h_left + 1e-9
        and t['row_mpm'] > 0 and t['count'] >= t['per_x']
    ]
    results: List[Tuple[Tuple[Tuple[int, int], ...], float]] = []
    seen: Set[Tuple] = set()

    def dfs(pos: int, width_left: float, mpm: float, stack: List[Tuple[int, int]]) -> None:
        if len(results) >= max_bands:
            return
        if mpm + 1e-9 >= mpm_lo:
            sig = tuple(sorted(stack))
            if sig not in seen:
                seen.add(sig)
                results.append((sig, mpm))
            return  # 已达下限就收口，不再加行（控制溢出与规模）
        for i in range(pos, len(rowable)):
            t = rowable[i]
            if t['row_width'] > width_left + 1e-9:
                continue
            max_rows_w = int((width_left + 1e-9) // t['row_width'])
            max_rows_inv = t['count'] // t['per_x']
            max_rows_mpm = int((mpm_hi - mpm + 1e-9) // t['row_mpm']) if t['row_mpm'] > 0 else 0
            top = min(max_rows_w, max_rows_inv, max_rows_mpm)
            for rows in range(1, top + 1):
                stack.append((t['idx'], rows))
                dfs(i + 1, width_left - rows * t['row_width'], mpm + rows * t['row_mpm'], stack)
                stack.pop()
                if len(results) >= max_bands:
                    return

    dfs(0, pallet_width, 0.0, [])
    return results


def enumerate_recipes(
    types: List[Dict],
    pallet_dims: Dict[str, float],
    target_mpm: float,
    max_overflow: float = 32.0,
    max_recipes: int = 30000,
) -> List[Dict]:
    """枚举配方：整层堆叠 + 至多一个顶带。

    Returns:
        配方列表，每项含 layers/band/mpm/waste/use（type_idx -> 箱数）。
    """
    pallet_height = float(pallet_dims.get('height', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    layerable = [
        t for t in types
        if t['per_layer'] > 0 and t['layer_mpm'] > 0
        and t['height'] <= pallet_height and t['count'] >= t['per_layer']
    ]
    recipes: List[Dict] = []
    seen: Set[Tuple] = set()
    band_memo: Dict[Tuple, List] = {}
    type_map = {t['idx']: t for t in types}

    def record(layer_stack: List[Tuple[int, int]], band_sig, mpm: float) -> None:
        sig = (tuple(sorted(layer_stack)), band_sig or ())
        if sig in seen or len(recipes) >= max_recipes:
            return
        seen.add(sig)
        use: Dict[int, int] = {}
        for ti, n_layers in sig[0]:
            use[ti] = use.get(ti, 0) + n_layers * type_map[ti]['per_layer']
        for ti, n_rows in sig[1]:
            use[ti] = use.get(ti, 0) + n_rows * type_map[ti]['per_x']
        recipes.append({
            'layers': sig[0],
            'band': sig[1],
            'mpm': mpm,
            'waste': mpm - target_mpm,
            'use': use,
        })

    def close_with_band(stack: List[Tuple[int, int]], h_left: float, mpm: float) -> None:
        """当前层堆叠不足 target 时，尝试用一个顶带收口。"""
        if not stack or mpm + 1e-9 >= target_mpm:
            return
        lo = target_mpm - mpm
        hi = target_mpm + max_overflow - mpm
        memo_key = (round(h_left, 3), round(lo, 3), round(hi, 3))
        if memo_key not in band_memo:
            band_memo[memo_key] = _enumerate_top_bands(
                types, h_left, pallet_width, lo, hi
            )
        for band_sig, band_mpm in band_memo[memo_key]:
            record(list(stack), band_sig, mpm + band_mpm)

    def dfs(pos: int, h_left: float, mpm: float, stack: List[Tuple[int, int]]) -> None:
        if len(recipes) >= max_recipes:
            return
        close_with_band(stack, h_left, mpm)
        for i in range(pos, len(layerable)):
            t = layerable[i]
            h = t['height']
            if h > h_left + 1e-9:
                continue
            layer_mpm = t['layer_mpm']
            max_k_h = int((h_left + 1e-9) // h)
            max_k_inv = t['count'] // t['per_layer']
            k_cap = min(max_k_h, max_k_inv)
            if k_cap <= 0:
                continue
            for k in range(1, k_cap + 1):
                new_mpm = mpm + k * layer_mpm
                if new_mpm > target_mpm + max_overflow + 1e-9:
                    # 仅允许"最后一跳"跨过 target（上一步还差着 target）
                    if new_mpm - layer_mpm < target_mpm - 1e-9:
                        stack.append((t['idx'], k))
                        record(list(stack), None, new_mpm)
                        stack.pop()
                    break
                stack.append((t['idx'], k))
                if new_mpm + 1e-9 >= target_mpm:
                    record(list(stack), None, new_mpm)
                else:
                    dfs(i + 1, h_left - k * h, new_mpm, stack)
                stack.pop()
                if len(recipes) >= max_recipes:
                    return

    dfs(0, pallet_height, 0.0, [])
    return recipes


def allocate_recipes(
    recipes: List[Dict],
    types: List[Dict],
    restarts: int = 400,
    seed: int = 0,
) -> List[Tuple[int, int]]:
    """库存约束下最大化配方实例数。

    Returns:
        [(recipe_idx, n_instances)]，按贪心选取顺序排列。
    """
    if not recipes:
        return []
    counts0 = {t['idx']: t['count'] for t in types}
    scarcity = {ti: 1.0 / max(1, c) for ti, c in counts0.items()}
    rng = random.Random(seed)
    n = len(recipes)

    def scarcity_cost(i: int) -> float:
        return sum(u * scarcity[ti] for ti, u in recipes[i]['use'].items())

    best_plan: List[Tuple[int, int]] = []
    best_total = 0
    for restart in range(restarts):
        if restart == 0:
            order = sorted(range(n), key=lambda i: (recipes[i]['waste'], scarcity_cost(i)))
        elif restart == 1:
            order = sorted(range(n), key=lambda i: (scarcity_cost(i), recipes[i]['waste']))
        else:
            w_waste = rng.uniform(0.0, 2.0)
            w_scar = rng.uniform(0.0, 60.0)
            order = sorted(
                range(n),
                key=lambda i: (
                    recipes[i]['waste'] * w_waste
                    + scarcity_cost(i) * w_scar
                    + rng.random() * 2.0
                ),
            )
        counts = dict(counts0)
        plan: List[Tuple[int, int]] = []
        total = 0
        for i in order:
            use = recipes[i]['use']
            k = min(counts[ti] // u for ti, u in use.items())
            if k <= 0:
                continue
            for ti, u in use.items():
                counts[ti] -= u * k
            plan.append((i, k))
            total += k
        if total > best_total:
            best_total = total
            best_plan = plan
    return best_plan


def describe_recipe(recipe: Dict, types: List[Dict]) -> str:
    """配方的人类可读描述。"""
    type_map = {t['idx']: t for t in types}
    parts = []
    for ti, n_layers in recipe['layers']:
        t = type_map[ti]
        parts.append(
            '%d层x[%gx%gx%g mpm%g ×%d/层=%g]'
            % (n_layers, t['length'], t['width'], t['height'],
               t['mpm'], t['per_layer'], t['layer_mpm'])
        )
    for ti, n_rows in recipe['band']:
        t = type_map[ti]
        parts.append(
            '顶带%d行x[%gx%gx%g mpm%g ×%d/行=%g]'
            % (n_rows, t['length'], t['width'], t['height'],
               t['mpm'], t['per_x'], t['row_mpm'])
        )
    return ' + '.join(parts) + ('  (mpm=%g)' % recipe['mpm'])


def plan_recipe_pools(
    boxes: List[Dict],
    target_mpm: Optional[float],
    pallet_dims: Dict[str, float],
    max_overflow: float = 32.0,
    banned_signatures: Optional[Set[Tuple]] = None,
    restarts: int = 400,
    seed: int = 0,
) -> Tuple[List[List[Dict]], Dict]:
    """端到端规划：类型表 → 配方枚举 → 配额分配 → 实例化箱池。

    Args:
        banned_signatures: 验证失败的配方签名集合 {(layers, band)}，重规划时跳过。

    Returns:
        (pools, meta)：pools = 每个配方实例的精确箱列表；
        meta 含 types/recipes/plan/pool_recipes（与 pools 一一对应）。
    """
    if not boxes or target_mpm is None or target_mpm <= 0:
        return [], {'types': [], 'recipes': [], 'plan': [], 'pool_recipes': []}

    types = build_type_table(boxes, pallet_dims)
    recipes = enumerate_recipes(types, pallet_dims, target_mpm, max_overflow)
    if banned_signatures:
        recipes = [
            r for r in recipes
            if (r['layers'], r['band']) not in banned_signatures
        ]
    plan = allocate_recipes(recipes, types, restarts=restarts, seed=seed)

    type_map = {t['idx']: t for t in types}
    consumed = {t['idx']: 0 for t in types}
    pools: List[List[Dict]] = []
    pool_recipes: List[Dict] = []
    for recipe_idx, n_instances in plan:
        recipe = recipes[recipe_idx]
        for _ in range(n_instances):
            pool: List[Dict] = []
            for ti in sorted(recipe['use']):
                need = recipe['use'][ti]
                start = consumed[ti]
                pool.extend(type_map[ti]['boxes'][start:start + need])
                consumed[ti] = start + need
            pools.append(pool)
            pool_recipes.append(recipe)
    return pools, {
        'types': types,
        'recipes': recipes,
        'plan': plan,
        'pool_recipes': pool_recipes,
    }
