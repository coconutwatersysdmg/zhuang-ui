"""
分层感知候选池构建器

按 (L,W,H,mpm) 分组，枚举每组的"放 N 个箱子要多高的整层堆叠"作为选项，
用多选背包搜索找到几何可行 + mpm ≈ target 的单托盘箱子组合。

供 direct-layer packer / rescue 链调用。算法纯函数式，无外部副作用。
"""

import math
import random
from typing import Dict, List, Optional

from .stacking_policy import (
    build_height_multiple_bonus_by_group,
    build_height_multiple_bonus_by_size,
    stacking_tiebreak_key,
)

def build_layer_aware_candidate_pool(
    items: List[Dict],
    target_mpm: Optional[float],
    pallet_dims: Dict[str, float],
    seed: int = 0,
    xy_tolerance: float = 2.0,
    z_tolerance: float = 0.0,
    max_overflow: Optional[float] = None,
    candidate_count: int = 1,
    prefer_fill: bool = False,
):
    """构建一个或多个 layer-compatible 候选池。

    index 桶只优化 mpm 总和，这里增加几何过滤：搜索"同类型整层切片"组合，
    使其有效 footprint 与累计高度都能塞进 pallet_dims。

    Args:
        items: 候选物品。
        target_mpm: 目标 mpm；None 或非正时返回 []。
        pallet_dims: 托盘尺寸。
        seed: 随机种子（影响同类内打乱顺序）。
        xy_tolerance / z_tolerance: 几何膨胀容差。
        max_overflow: 允许超过 target 的 mpm 上限；默认 max(32, target*0.25)。
        candidate_count: 返回候选池数量。
            - <=1: 返回单个 List[Dict]
            - >1: 返回 List[List[Dict]]
    """
    if not items or target_mpm is None or target_mpm <= 0 or not pallet_dims:
        return []

    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    pallet_height = float(pallet_dims.get('height', 0) or 0)
    if pallet_length <= 0 or pallet_width <= 0 or pallet_height <= 0:
        return []

    if max_overflow is None:
        max_overflow = max(32.0, target_mpm * 0.25)

    grouped: Dict[tuple, List[Dict]] = {}
    for item in items:
        mpm = float(item.get('min_pack_multiple', 0) or 0)
        length = float(item.get('length', 0) or 0)
        width = float(item.get('width', 0) or 0)
        height = float(item.get('height', 0) or 0)
        if mpm <= 0 or length <= 0 or width <= 0 or height <= 0:
            continue
        key = (length, width, height, mpm)
        grouped.setdefault(key, []).append(item)

    if not grouped:
        return []

    rng = random.Random(seed)
    grouped_options: List[List[Dict]] = []
    grouped_items: Dict[tuple, List[Dict]] = {}
    group_multiple_bonus = build_height_multiple_bonus_by_group(items)
    size_multiple_bonus = build_height_multiple_bonus_by_size(items)
    height_scale = 10
    mpm_scale = 10
    height_limit = int(math.floor(pallet_height * height_scale + 1e-9))
    target_int = int(math.ceil(target_mpm * mpm_scale - 1e-9))
    max_mpm_int = int(math.floor((target_mpm + max_overflow) * mpm_scale + 1e-9))

    for key, group in grouped.items():
        length, width, height, mpm = key
        effective_length = length + xy_tolerance
        effective_width = width + xy_tolerance
        effective_height = height + z_tolerance
        if (
            effective_length > pallet_length + 1e-9
            or effective_width > pallet_width + 1e-9
            or effective_height > pallet_height + 1e-9
        ):
            continue

        per_layer = (
            int(pallet_length // effective_length)
            * int(pallet_width // effective_width)
        )
        if per_layer <= 0:
            continue
        max_layers = int(pallet_height // effective_height)
        max_count = min(len(group), per_layer * max_layers)
        if max_count <= 0:
            continue

        ordered_group = sorted(group, key=lambda b: str(b.get('id')))
        rng.shuffle(ordered_group)
        ordered_group.sort(
            key=lambda item: stacking_tiebreak_key(item, size_multiple_bonus)
        )
        grouped_items[key] = ordered_group

        single_type_capacity_mpm = max_count * mpm
        hard_weight = 0.0
        if single_type_capacity_mpm + 1e-9 < target_mpm:
            hard_weight = (
                (target_mpm - single_type_capacity_mpm)
                / max(target_mpm, 1.0)
            )

        useful_counts = set()
        first_layer_limit = min(per_layer, max_count, 32)
        useful_counts.update(range(1, first_layer_limit + 1))
        useful_counts.update(range(per_layer, max_count + 1, per_layer))
        useful_counts.add(max_count)
        target_count = int(math.ceil(target_mpm / mpm))
        target_window = min(per_layer, 16)
        for count in range(
            max(1, target_count - target_window),
            min(max_count, target_count + target_window) + 1,
        ):
            useful_counts.add(count)

        options = []
        for count in sorted(useful_counts):
            layer_count = int(math.ceil(count / per_layer))
            used_height = layer_count * effective_height
            if used_height > pallet_height + 1e-9:
                continue
            mpm_total = count * mpm
            mpm_int = int(round(mpm_total * mpm_scale))
            if mpm_int > max_mpm_int:
                continue
            height_int = int(math.ceil(used_height * height_scale - 1e-9))
            footprint_fill = (
                count * effective_length * effective_width
                / max(layer_count * pallet_length * pallet_width, 1.0)
            )
            options.append({
                "key": key,
                "count": count,
                "height_int": height_int,
                "mpm_int": mpm_int,
                "mpm_total": mpm_total,
                "footprint_fill": footprint_fill,
                "hard_score": mpm_total * hard_weight,
                "multiple_score": count * group_multiple_bonus.get(key, 0.0),
            })
        if len(options) > 72:
            options = sorted(
                options,
                key=lambda option: (
                    -option["hard_score"] if prefer_fill else 0.0,
                    -option["multiple_score"],
                    abs(target_int - option["mpm_int"]),
                    option["height_int"],
                    -option["footprint_fill"],
                    option["count"],
                ),
            )[:72]
        if options:
            grouped_options.append(options)

    if not grouped_options:
        return []

    def _prune_states(states: Dict, limit: int = 1200) -> Dict:
        if len(states) <= limit:
            return states
        ranked = sorted(
            states.items(),
            key=lambda kv: (
                -kv[1].get("hard", 0.0) if prefer_fill else 0.0,
                -kv[1].get("multiple", 0.0),
                abs(target_int - kv[0][1]),
                kv[0][1] < target_int,
                kv[0][0],
                -kv[1]["fill"],
                kv[1]["count"],
            ),
        )
        return dict(ranked[:limit])

    # Multi-choice knapsack: at most one count option per box type.
    dp = {
        (0, 0): {
            "fill": 0.0,
            "hard": 0.0,
            "multiple": 0.0,
            "count": 0,
            "choices": [],
        }
    }
    for options in grouped_options:
        next_dp = dict(dp)
        for (used_height, used_mpm), state in dp.items():
            for option in options:
                next_height = used_height + option["height_int"]
                next_mpm = used_mpm + option["mpm_int"]
                if next_height > height_limit or next_mpm > max_mpm_int:
                    continue
                next_state = {
                    "fill": state["fill"] + option["footprint_fill"],
                    "hard": state.get("hard", 0.0) + option["hard_score"],
                    "multiple": (
                        state.get("multiple", 0.0) + option["multiple_score"]
                    ),
                    "count": state["count"] + option["count"],
                    "choices": state["choices"] + [option],
                }
                old = next_dp.get((next_height, next_mpm))
                if old is None or (
                    next_state["hard"],
                    next_state["multiple"],
                    next_state["fill"],
                    -next_state["count"],
                ) > (
                    old.get("hard", 0.0),
                    old.get("multiple", 0.0),
                    old["fill"],
                    -old["count"],
                ):
                    next_dp[(next_height, next_mpm)] = next_state
        dp = _prune_states(next_dp)

    ranked_candidates = []
    for (used_height, used_mpm), state in dp.items():
        if used_mpm < target_int or not state["choices"]:
            continue
        if prefer_fill:
            key = (
                -state.get("hard", 0.0),
                -state.get("multiple", 0.0),
                -state["fill"],
                -state["count"],
                used_mpm - target_int,
                used_height,
            )
        else:
            key = (
                used_mpm - target_int,
                -state.get("multiple", 0.0),
                -state.get("hard", 0.0),
                state["count"],
                used_height,
                -state["fill"],
            )
        ranked_candidates.append((key, state))

    if not ranked_candidates:
        return []

    def _selected_from_state(state: Dict) -> List[Dict]:
        selected: List[Dict] = []
        selected_ids = set()
        for choice in state["choices"]:
            for item in grouped_items[choice["key"]][:choice["count"]]:
                item_id = item.get('id')
                if item_id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item_id)
        selected.sort(
            key=lambda b: (
                stacking_tiebreak_key(b, size_multiple_bonus),
                -float(b.get('length', 0) or 0) * float(b.get('width', 0) or 0),
                -float(b.get('min_pack_multiple', 0) or 0),
                str(b.get('id')),
            )
        )
        return selected

    ranked_candidates.sort(key=lambda x: x[0])
    if candidate_count <= 1:
        return _selected_from_state(ranked_candidates[0][1])

    pools: List[List[Dict]] = []
    seen_signatures = set()
    for _, state in ranked_candidates:
        pool = _selected_from_state(state)
        signature = tuple(sorted(str(item.get('id')) for item in pool))
        if signature in seen_signatures:
            continue
        pools.append(pool)
        seen_signatures.add(signature)
        if len(pools) >= candidate_count:
            break
    return pools
