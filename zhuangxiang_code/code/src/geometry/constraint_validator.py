"""Full pallet constraint validation.

This module is used as a stage gate after every packing or rescue mutation.
It does not repair a pallet.  It only reports whether the current placement
still satisfies the hard geometric and handling constraints.
"""

from typing import Dict, List

from .center_of_mass import validate_center_of_mass
from .gap_checker import passes_box_gap_constraint
from .overlap import axis_overlap_len
from .support import direct_support_ratio
from ..utils.case_group import find_case_group_violation
from ..utils.helpers import passes_small_box_not_on_larger_constraint


REQUIRED_SUCTION_FIELDS = (
    "suction_box_corner",
    "suction_cup_corner",
    "suction_orientation",
    "suction_cup_x_size",
    "suction_cup_y_size",
    "suction_rect_x_min",
    "suction_rect_x_max",
    "suction_rect_y_min",
    "suction_rect_y_max",
)


def validate_pallet_constraints(
    pallet_plan: Dict,
    pallet_dims: Dict[str, float],
    support_ratio_threshold: float = 0.8,
    max_gap: float = 6.0,
    require_suction: bool = True,
    center_of_mass_tolerance: float = 1.0 / 3.0,
    small_box_below_enabled: bool = True,
    constraint_config=None,
    target_mpm=None,
) -> Dict:
    """校验单个托盘方案的全部硬约束。

    必须约束（恒查，仅阈值可配）：越界、重叠、间隙、支撑率、重心稳定。
    可关约束：小箱在下（small_box_below_enabled）、吸盘字段（require_suction）。

    Args:
        center_of_mass_tolerance: 重心相对托盘中心的最大允许偏移比例。
        small_box_below_enabled: 是否校验「小箱在下」。
        constraint_config: 可选 ConstraintConfig，提供时统一覆盖上述阈值/开关，
            保证门禁与放置层同源。不提供时沿用各参数默认值（行为不变）。
        target_mpm: 可选目标指数。提供且整盘指数 ≥ target 时跳过 gap 间隙校验
            （达标盘免 gap：gap 约束本意是防止偷懒留大空隙，达标即已尽力装满，
            剩余空隙是高密度装载的几何必然）；未达标盘仍查 gap。None 时永远查
            gap（历史行为不变）。

    注：「同尺寸重箱在下」是放置时偏好约束，不在整盘硬门禁内校验，以保持
    门禁与历史行为一致；其开关在放置层（BeamSearchPacker/direct_layer）生效。
    """
    if constraint_config is not None:
        support_ratio_threshold = constraint_config.support_ratio_threshold
        max_gap = constraint_config.max_box_gap_mm
        require_suction = constraint_config.suction_reachability_enabled
        center_of_mass_tolerance = constraint_config.center_of_mass_tolerance
        small_box_below_enabled = constraint_config.small_box_below_enabled
    violations: List[Dict] = []
    items = pallet_plan.get("packed_items", []) or []
    # case_group 同组约束（必须约束，数据驱动：全 0/缺失时永不触发）：
    # 非 0 case_group 的箱子只能与相同值的箱子同托盘。分组隔离已结构性保证，
    # 此处为门禁层保险（与放置层同源），防未来跨组重排破坏隔离。
    cg_violation = find_case_group_violation(items)
    if cg_violation:
        violations.append({
            "type": "case_group_mixed",
            "detail": cg_violation,
        })
    # 达标盘免 gap 校验（用户决策）：gap 约束本意是防止装箱偷懒留大空隙导致装
    # 不满；整盘指数达标即已尽力装满，剩余空隙是高密度装载的几何必然，不再以
    # gap 判失败。未达标盘仍查 gap（防偷懒）。越界/重叠/支撑/重心/吸盘恒查，
    # 物理稳定性不受影响。
    _total_mpm = sum(float(it.get("min_pack_multiple", 0) or 0) for it in items)
    _gap_exempt = target_mpm is not None and _total_mpm + 1e-9 >= float(target_mpm)
    pallet_length = float(pallet_dims.get("length", 0) or 0)
    pallet_width = float(pallet_dims.get("width", 0) or 0)
    pallet_height = float(pallet_dims.get("height", 0) or 0)

    for idx, item in enumerate(items):
        item_id = item.get("id")
        pos = item.get("position")
        if not pos:
            violations.append({
                "type": "missing_position",
                "box_id": item_id,
            })
            continue

        dims = _dims(item)
        if (
            pos.get("x", 0) < -1e-9
            or pos.get("y", 0) < -1e-9
            or pos.get("z", 0) < -1e-9
            or pos.get("x", 0) + dims["length"] > pallet_length + 1e-9
            or pos.get("y", 0) + dims["width"] > pallet_width + 1e-9
            or pos.get("z", 0) + dims["height"] > pallet_height + 1e-9
        ):
            violations.append({
                "type": "out_of_bounds",
                "box_id": item_id,
            })

        if require_suction:
            missing = [
                field for field in REQUIRED_SUCTION_FIELDS
                if item.get(field) is None
            ]
            if missing:
                violations.append({
                    "type": "missing_suction",
                    "box_id": item_id,
                    "fields": missing,
                })

        others = [
            other for other in items
            if other.get("id") != item_id and other.get("position")
        ]
        raw = {
            "length": float(item.get("raw_length", item.get("length", 0)) or 0),
            "width": float(item.get("raw_width", item.get("width", 0)) or 0),
            "height": float(item.get("raw_height", item.get("height", 0)) or 0),
        }
        if not _gap_exempt and not passes_box_gap_constraint(
            pos, dims, raw, others, max_gap=max_gap,
            pallet_dims=pallet_dims,
        ):
            violations.append({
                "type": "gap",
                "box_id": item_id,
            })

        if pos.get("z", 0) > 1e-9:
            ratio = direct_support_ratio(pos, dims, others)
            if ratio + 1e-9 < support_ratio_threshold:
                violations.append({
                    "type": "support",
                    "box_id": item_id,
                    "support_ratio": ratio,
                })

        # 小箱在下（可关约束）：小箱不得直接置于体积更大的箱子之上
        if small_box_below_enabled and not (
            passes_small_box_not_on_larger_constraint(item, pos, dims, others)
        ):
            violations.append({
                "type": "small_box_on_larger",
                "box_id": item_id,
            })

        for other in items[idx + 1:]:
            other_pos = other.get("position")
            if not other_pos:
                continue
            other_dims = _dims(other)
            if (
                axis_overlap_len(
                    pos["x"], pos["x"] + dims["length"],
                    other_pos["x"], other_pos["x"] + other_dims["length"],
                ) > 1e-9
                and axis_overlap_len(
                    pos["y"], pos["y"] + dims["width"],
                    other_pos["y"], other_pos["y"] + other_dims["width"],
                ) > 1e-9
                and axis_overlap_len(
                    pos["z"], pos["z"] + dims["height"],
                    other_pos["z"], other_pos["z"] + other_dims["height"],
                ) > 1e-9
            ):
                violations.append({
                    "type": "overlap",
                    "box_id": item_id,
                    "other_box_id": other.get("id"),
                })

    if items and not any(v["type"] == "missing_position" for v in violations):
        com = validate_center_of_mass(
            pallet_plan, pallet_dims, tolerance=center_of_mass_tolerance
        )
        if not com.get("is_stable", False):
            violations.append({
                "type": "center_of_mass",
                "detail": com,
            })

    return {
        "is_valid": not violations,
        "violations": violations,
    }


def validate_plan_constraints(
    plans: List[Dict],
    pallet_dims: Dict[str, float],
    **kwargs,
) -> Dict:
    """Validate all non-empty pallets in a group."""
    invalid = []
    for plan in plans:
        if not plan.get("packed_items"):
            continue
        result = validate_pallet_constraints(plan, pallet_dims, **kwargs)
        if not result["is_valid"]:
            invalid.append({
                "pallet_id": plan.get("pallet_id"),
                "violations": result["violations"],
            })
    return {
        "is_valid": not invalid,
        "invalid_pallets": invalid,
    }


def _dims(item: Dict) -> Dict[str, float]:
    return {
        "length": float(item.get("length", 0) or 0),
        "width": float(item.get("width", 0) or 0),
        "height": float(item.get("height", 0) or 0),
    }
