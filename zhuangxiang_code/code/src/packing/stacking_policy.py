"""Shared stacking preferences and constraints for packing flows."""

from typing import Dict, Iterable, List, Tuple

from src.geometry.overlap import has_positive_xy_overlap
from src.utils.dimensions import raw_dims as get_raw_dims

_HEIGHT_MULTIPLE_TOL_MM = 1.0
_MAX_HEIGHT_RATIO = 6


def _round_mm(value: float) -> float:
    return round(float(value or 0.0), 6)


def _weight(item: Dict) -> float:
    return float(item.get("weight", 0.0) or 0.0)


def size_key(item: Dict) -> Tuple[float, float, float]:
    raw = get_raw_dims(item)
    return (
        _round_mm(raw["length"]),
        _round_mm(raw["width"]),
        _round_mm(raw["height"]),
    )


def footprint_key(item: Dict) -> Tuple[float, float]:
    raw = get_raw_dims(item)
    return (_round_mm(raw["length"]), _round_mm(raw["width"]))


def group_key(item: Dict) -> Tuple[float, float, float, float]:
    raw = get_raw_dims(item)
    return (
        _round_mm(raw["length"]),
        _round_mm(raw["width"]),
        _round_mm(raw["height"]),
        _round_mm(float(item.get("min_pack_multiple", 0.0) or 0.0)),
    )


def sort_same_size_heavier_first(items: Iterable[Dict]) -> List[Dict]:
    return sorted(
        items,
        key=lambda item: (
            -_weight(item),
            str(item.get("id")),
        ),
    )


def build_height_multiple_bonus_by_size(
    items: Iterable[Dict],
) -> Dict[Tuple[float, float, float], float]:
    items = list(items)
    families: Dict[Tuple[float, float], set] = {}
    for item in items:
        fp_key = footprint_key(item)
        sz_key = size_key(item)
        families.setdefault(fp_key, set()).add(sz_key[2])

    bonus_by_size: Dict[Tuple[float, float, float], float] = {}
    for item in items:
        sz_key = size_key(item)
        family_heights = families.get(footprint_key(item), set())
        bonus = 0.0
        for other_height in family_heights:
            if abs(other_height - sz_key[2]) <= 1e-9:
                continue
            bonus = max(
                bonus,
                _height_multiple_score(sz_key[2], other_height),
            )
        bonus_by_size[sz_key] = bonus
    return bonus_by_size


def build_height_multiple_bonus_by_group(
    items: Iterable[Dict],
) -> Dict[Tuple[float, float, float, float], float]:
    items = list(items)
    size_bonus = build_height_multiple_bonus_by_size(items)
    bonus_by_group: Dict[Tuple[float, float, float, float], float] = {}
    for item in items:
        bonus_by_group[group_key(item)] = size_bonus.get(size_key(item), 0.0)
    return bonus_by_group


def stacking_tiebreak_key(
    item: Dict,
    size_bonus: Dict[Tuple[float, float, float], float],
) -> Tuple[float, float, float, str]:
    item_size = size_key(item)
    bonus = size_bonus.get(item_size, 0.0)
    return (
        -bonus,
        -item_size[2] if bonus > 0.0 else 0.0,
        -_weight(item),
        str(item.get("id")),
    )


def passes_same_size_heavier_below_constraint(
    item: Dict,
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_boxes: List[Dict],
    eps: float = 1e-6,
) -> bool:
    if point["z"] <= eps:
        return True

    candidate_weight = _weight(item)
    if candidate_weight <= 0.0:
        return True

    candidate_size = size_key(item)
    for support in placed_boxes:
        support_pos = support.get("position")
        if not support_pos:
            continue
        support_top = float(support_pos["z"]) + float(support.get("height", 0.0) or 0.0)
        if abs(support_top - point["z"]) > eps:
            continue
        if not has_positive_xy_overlap(point, dims, support, eps=eps):
            continue
        if size_key(support) != candidate_size:
            continue
        support_weight = _weight(support)
        if support_weight <= 0.0:
            continue
        if candidate_weight > support_weight + eps:
            return False
    return True


def _height_multiple_score(height: float, other_height: float) -> float:
    small = min(height, other_height)
    large = max(height, other_height)
    if small <= 0.0:
        return 0.0
    ratio = large / small
    nearest = int(round(ratio))
    if nearest < 2 or nearest > _MAX_HEIGHT_RATIO:
        return 0.0
    if abs(large - small * nearest) > _HEIGHT_MULTIPLE_TOL_MM:
        return 0.0
    score = float(nearest)
    if height >= other_height - _HEIGHT_MULTIPLE_TOL_MM:
        score += 0.25
    return score
