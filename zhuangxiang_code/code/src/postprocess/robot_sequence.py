"""Robot execution sequence post-processor for completed pallet layouts.

The packing algorithm remains responsible for the final geometry. This module
only enriches successful pallets with a deterministic execution order suitable
for a vertical place motion with a fixed 600 x 800 mm suction plate. The plate
and the box always use their ``x_min_y_min`` corner as the common anchor.

The sequence is generated in two layers:

* hard dependencies: direct support plus suction/body vertical clearance;
* robot-relative retreat policy: finish the farthest unfinished depth band and
  its complete predecessor chain before allowing unrelated near-side boxes.

This prevents a common industrial failure mode where large or tall near-side
boxes are placed early and the robot later has to reach over them to serve an
unfinished far-side target.

Robot inverse kinematics and full-link collision checks are intentionally not
claimed here. Their status remains ``robot_motion_verified=False``.
"""

from __future__ import annotations

from collections import defaultdict
from math import hypot, sqrt
from typing import Callable, Dict, Iterable, List, MutableMapping, Sequence, Set, Tuple

SUCTION_CUP_X_MM = 600.0
SUCTION_CUP_Y_MM = 800.0
FIXED_CORNER = "x_min_y_min"
ROBOT_REFERENCE = "x_min_y_min"
DEPTH_BAND_COUNT = 4
SEQUENCE_STRATEGY = "robot_relative_far_to_near_retreat"
DEPTH_POLICY = "farthest_unfinished_band_locked"
_VALID_ROBOT_REFERENCES = {
    "x_min_y_min",
    "x_max_y_min",
    "x_min_y_max",
    "x_max_y_max",
    "x_min",
    "x_max",
    "y_min",
    "y_max",
}


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _item_id(item: Dict, index: int) -> str:
    value = item.get("id")
    return str(value if value is not None else f"BOX-{index + 1}")


def _geometry(item: Dict) -> Dict[str, float]:
    pos = item.get("position") or {}
    x0 = _number(pos.get("x"))
    y0 = _number(pos.get("y"))
    z0 = _number(pos.get("z"))
    lx = max(0.0, _number(item.get("length")))
    ly = max(0.0, _number(item.get("width")))
    lz = max(0.0, _number(item.get("height")))
    return {
        "x0": x0,
        "x1": x0 + lx,
        "y0": y0,
        "y1": y0 + ly,
        "z0": z0,
        "z1": z0 + lz,
        "lx": lx,
        "ly": ly,
        "lz": lz,
    }


def _rect_overlap(a: Dict[str, float], b: Dict[str, float], tolerance: float = 1e-9) -> bool:
    return (
        min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]) > tolerance
        and min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]) > tolerance
    )


def _rect_overlap_area(a: Dict[str, float], b: Dict[str, float]) -> float:
    dx = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    dy = max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))
    return dx * dy


def _suction_rect(geom: Dict[str, float], cup_x: float, cup_y: float) -> Dict[str, float]:
    return {
        "x0": geom["x0"],
        "x1": geom["x0"] + cup_x,
        "y0": geom["y0"],
        "y1": geom["y0"] + cup_y,
    }


def _expanded_body_rect(geom: Dict[str, float], clearance: float) -> Dict[str, float]:
    # Expansion is only used when the other obstacle is taller than the target.
    # The strict height direction prevents symmetric adjacency cycles.
    return {
        "x0": geom["x0"] - clearance,
        "x1": geom["x1"] + clearance,
        "y0": geom["y0"] - clearance,
        "y1": geom["y1"] + clearance,
    }


def _validate_robot_reference(value: str) -> str:
    reference = str(value or ROBOT_REFERENCE).strip().lower()
    if reference not in _VALID_ROBOT_REFERENCES:
        allowed = ", ".join(sorted(_VALID_ROBOT_REFERENCES))
        raise ValueError(f"unsupported robot_reference={value!r}; allowed: {allowed}")
    return reference


def _robot_depth(
    geom: Dict[str, float],
    pallet_length: float,
    pallet_width: float,
    robot_reference: str,
) -> float:
    """Return normalized robot-relative target depth in [0, 1].

    Side references use one-dimensional depth. Corner references use normalized
    Euclidean distance from the selected pallet corner to the box footprint
    centre. Using the centre rather than only ``x_min/y_min`` prevents small and
    large neighbouring boxes from receiving misleadingly identical depths.
    """
    cx = (geom["x0"] + geom["x1"]) / 2.0
    cy = (geom["y0"] + geom["y1"]) / 2.0
    x_from_min = min(1.0, max(0.0, cx / pallet_length))
    x_from_max = min(1.0, max(0.0, (pallet_length - cx) / pallet_length))
    y_from_min = min(1.0, max(0.0, cy / pallet_width))
    y_from_max = min(1.0, max(0.0, (pallet_width - cy) / pallet_width))

    if robot_reference == "x_min":
        return x_from_min
    if robot_reference == "x_max":
        return x_from_max
    if robot_reference == "y_min":
        return y_from_min
    if robot_reference == "y_max":
        return y_from_max

    x_depth = x_from_min if robot_reference.startswith("x_min") else x_from_max
    y_depth = y_from_min if robot_reference.endswith("y_min") else y_from_max
    return min(1.0, hypot(x_depth, y_depth) / sqrt(2.0))


def _depth_band(depth: float, band_count: int) -> int:
    count = max(1, int(band_count))
    bounded = min(1.0, max(0.0, float(depth)))
    return min(count - 1, int(bounded * count))


def _transitive_predecessors(
    node_ids: Sequence[str],
    predecessors: MutableMapping[str, Set[str]],
) -> Dict[str, Set[str]]:
    node_set = set(node_ids)
    memo: Dict[str, Set[str]] = {}
    visiting: Set[str] = set()

    def visit(node_id: str) -> Set[str]:
        if node_id in memo:
            return set(memo[node_id])
        if node_id in visiting:
            # Cycle handling is left to the topological pass. Avoid recursion.
            return set()
        visiting.add(node_id)
        result: Set[str] = set()
        for pred in set(predecessors.get(node_id, set())) & node_set:
            result.add(pred)
            result.update(visit(pred))
        visiting.remove(node_id)
        memo[node_id] = result
        return set(result)

    return {node_id: visit(node_id) for node_id in node_ids}


def topological_sequence(
    node_ids: Sequence[str],
    predecessors: MutableMapping[str, Set[str]],
    score_key: Callable[[str, Set[str]], Tuple],
) -> Tuple[List[str], List[str]]:
    """Return a deterministic scored topological order and unresolved nodes.

    This generic helper remains available for unit tests and other callers. The
    pallet planner below uses the stricter far-target locking variant.
    """
    nodes = list(node_ids)
    node_set = set(nodes)
    original_rank = {node_id: i for i, node_id in enumerate(nodes)}
    clean_predecessors = {
        node_id: set(predecessors.get(node_id, set())) & node_set
        for node_id in nodes
    }
    placed: Set[str] = set()
    order: List[str] = []

    while len(order) < len(nodes):
        candidates = [
            node_id
            for node_id in nodes
            if node_id not in placed and clean_predecessors[node_id] <= placed
        ]
        if not candidates:
            remaining = sorted(
                (node_id for node_id in nodes if node_id not in placed),
                key=lambda node_id: original_rank[node_id],
            )
            return order, remaining
        chosen = min(candidates, key=lambda node_id: score_key(node_id, placed))
        placed.add(chosen)
        order.append(chosen)

    return order, []


def far_to_near_topological_sequence(
    node_ids: Sequence[str],
    predecessors: MutableMapping[str, Set[str]],
    depth_by_id: MutableMapping[str, float],
    depth_band_by_id: MutableMapping[str, int],
    score_key: Callable[[str, Set[str], Set[str]], Tuple],
) -> Tuple[List[str], List[str]]:
    """Topological order with farthest-unfinished-band locking.

    At every step, the planner identifies the deepest unfinished band. Only
    boxes in that band or in the transitive predecessor chain required to reach
    that band are considered. Unrelated near-side boxes remain locked out until
    the far band is complete.
    """
    nodes = list(node_ids)
    node_set = set(nodes)
    original_rank = {node_id: i for i, node_id in enumerate(nodes)}
    clean_predecessors = {
        node_id: set(predecessors.get(node_id, set())) & node_set
        for node_id in nodes
    }
    ancestors = _transitive_predecessors(nodes, clean_predecessors)
    placed: Set[str] = set()
    order: List[str] = []

    while len(order) < len(nodes):
        unplaced = [node_id for node_id in nodes if node_id not in placed]
        candidates = [
            node_id
            for node_id in unplaced
            if clean_predecessors[node_id] <= placed
        ]
        if not candidates:
            remaining = sorted(unplaced, key=lambda node_id: original_rank[node_id])
            return order, remaining

        farthest_band = max(depth_band_by_id[node_id] for node_id in unplaced)
        far_targets = {
            node_id
            for node_id in unplaced
            if depth_band_by_id[node_id] == farthest_band
        }
        required_chain: Set[str] = set(far_targets)
        for target_id in far_targets:
            required_chain.update(ancestors[target_id] - placed)

        focused_candidates = [
            node_id for node_id in candidates if node_id in required_chain
        ]
        candidate_pool = focused_candidates or candidates
        chosen = min(
            candidate_pool,
            key=lambda node_id: score_key(node_id, placed, far_targets),
        )
        placed.add(chosen)
        order.append(chosen)

    return order, []


def _reset_sequence_fields(
    item: Dict,
    original_sequence: int,
    cup_x: float,
    cup_y: float,
    robot_reference: str,
) -> None:
    geom = _geometry(item)
    item["original_packing_sequence"] = int(
        item.get("original_packing_sequence") or original_sequence
    )
    item["robot_packing_sequence"] = None
    item["suction_box_corner"] = FIXED_CORNER
    item["suction_cup_corner"] = FIXED_CORNER
    item["suction_orientation"] = f"cup_{cup_x:g}x_{cup_y:g}y"
    item["suction_cup_x_size"] = float(cup_x)
    item["suction_cup_y_size"] = float(cup_y)
    item["suction_rect_x_min"] = geom["x0"]
    item["suction_rect_x_max"] = geom["x0"] + cup_x
    item["suction_rect_y_min"] = geom["y0"]
    item["suction_rect_y_max"] = geom["y0"] + cup_y
    item["robot_reference"] = robot_reference
    item["robot_depth"] = None
    item["robot_depth_band"] = None
    item["support_predecessors"] = []
    item["clearance_predecessors"] = []
    item["body_clearance_predecessors"] = []
    item["all_predecessors"] = []
    item["clearance_successors"] = []
    item["geometric_sequence_feasible"] = False
    item["robot_motion_verified"] = False
    item["robot_validation_reason"] = "geometry_only_not_verified"


def plan_pallet_robot_sequence(
    pallet: Dict,
    *,
    cup_x: float = SUCTION_CUP_X_MM,
    cup_y: float = SUCTION_CUP_Y_MM,
    robot_reference: str = ROBOT_REFERENCE,
    depth_band_count: int = DEPTH_BAND_COUNT,
    support_z_tolerance_mm: float = 2.0,
    clearance_z_tolerance_mm: float = 1.0,
    body_xy_clearance_mm: float = 10.0,
) -> Dict:
    """Enrich one pallet with a fixed-corner far-to-near robot sequence.

    Failed pallets are deliberately untouched because they are not executable
    output pallets. Successful pallets preserve ``packed_items`` list order and
    final geometry; only metadata fields are added.
    """
    robot_reference = _validate_robot_reference(robot_reference)
    depth_band_count = max(1, int(depth_band_count))
    status = str(pallet.get("mpm_status") or "UNKNOWN").upper()
    items = pallet.get("packed_items") or []
    common_status = {
        "fixed_corner": FIXED_CORNER,
        "sequence_strategy": SEQUENCE_STRATEGY,
        "suction_cup_size": [float(cup_x), float(cup_y)],
        "robot_reference": robot_reference,
        "depth_band_count": depth_band_count,
        "depth_policy": DEPTH_POLICY,
    }
    if status != "SUCCESS":
        pallet.update({
            **common_status,
            "has_cycle": False,
            "constraint_count": 0,
            "sequence_status": "SKIPPED_FAILED_PALLET",
            "geometric_sequence_feasible": False,
            "robot_verified": False,
            "failed_box_ids": [],
        })
        return pallet

    if not items:
        pallet.update({
            **common_status,
            "has_cycle": False,
            "constraint_count": 0,
            "sequence_status": "SEQUENCE_ERROR_EMPTY_SUCCESS_PALLET",
            "geometric_sequence_feasible": False,
            "robot_verified": False,
            "failed_box_ids": [],
        })
        return pallet

    node_ids: List[str] = []
    item_by_id: Dict[str, Dict] = {}
    geom_by_id: Dict[str, Dict[str, float]] = {}
    original_rank: Dict[str, int] = {}
    duplicate_ids: List[str] = []

    for index, item in enumerate(items):
        node_id = _item_id(item, index)
        if node_id in item_by_id:
            duplicate_ids.append(node_id)
            node_id = f"{node_id}__SEQ_INDEX_{index + 1}"
        node_ids.append(node_id)
        item_by_id[node_id] = item
        geom_by_id[node_id] = _geometry(item)
        original_rank[node_id] = index + 1
        _reset_sequence_fields(item, index + 1, cup_x, cup_y, robot_reference)

    predecessors: Dict[str, Set[str]] = {node_id: set() for node_id in node_ids}
    support_predecessors: Dict[str, Set[str]] = defaultdict(set)
    suction_predecessors: Dict[str, Set[str]] = defaultdict(set)
    body_predecessors: Dict[str, Set[str]] = defaultdict(set)
    clearance_successors: Dict[str, Set[str]] = defaultdict(set)

    for target_id in node_ids:
        target = geom_by_id[target_id]
        target_suction = _suction_rect(target, cup_x, cup_y)
        target_body_sweep = _expanded_body_rect(target, body_xy_clearance_mm)

        for other_id in node_ids:
            if target_id == other_id:
                continue
            other = geom_by_id[other_id]

            # H1: every direct supporter below the target is a predecessor.
            if (
                target["z0"] > support_z_tolerance_mm
                and abs(other["z1"] - target["z0"]) <= support_z_tolerance_mm
                and _rect_overlap_area(target, other) > 1e-6
            ):
                predecessors[target_id].add(other_id)
                support_predecessors[target_id].add(other_id)

            # H2/H3: if a taller obstacle would intersect the vertical plate or
            # body sweep of the target, the target must be placed first.
            taller_than_target = other["z1"] > target["z1"] + clearance_z_tolerance_mm
            if taller_than_target and _rect_overlap(target_suction, other):
                predecessors[other_id].add(target_id)
                suction_predecessors[other_id].add(target_id)
                clearance_successors[target_id].add(other_id)
            if taller_than_target and _rect_overlap(target_body_sweep, other):
                predecessors[other_id].add(target_id)
                body_predecessors[other_id].add(target_id)
                clearance_successors[target_id].add(other_id)

    successors: Dict[str, Set[str]] = {node_id: set() for node_id in node_ids}
    for node_id, preds in predecessors.items():
        for pred in preds:
            successors[pred].add(node_id)

    dims = (items[0].get("pallet_dims") or {}) if items else {}
    pallet_length = max(1.0, _number(dims.get("length"), 1440.0))
    pallet_width = max(1.0, _number(dims.get("width"), 2240.0))
    pallet_height = max(1.0, _number(dims.get("height"), 720.0))
    pallet_area = pallet_length * pallet_width

    depth_by_id = {
        node_id: _robot_depth(
            geom_by_id[node_id], pallet_length, pallet_width, robot_reference
        )
        for node_id in node_ids
    }
    depth_band_by_id = {
        node_id: _depth_band(depth_by_id[node_id], depth_band_count)
        for node_id in node_ids
    }

    for node_id in node_ids:
        item_by_id[node_id]["robot_depth"] = round(depth_by_id[node_id], 9)
        item_by_id[node_id]["robot_depth_band"] = depth_band_by_id[node_id]

    def score_key(node_id: str, placed: Set[str], far_targets: Set[str]) -> Tuple:
        geom = geom_by_id[node_id]
        after = set(placed)
        after.add(node_id)
        unlock_far_count = sum(
            1
            for successor in successors[node_id]
            if successor in far_targets
            and successor not in after
            and predecessors[successor] <= after
        )
        unlock_total_count = sum(
            1
            for successor in successors[node_id]
            if successor not in after and predecessors[successor] <= after
        )
        # A large/tall near-side candidate is a potential access barrier. This
        # remains a soft tie-breaker because exact arm-link collision needs the
        # real robot model.
        footprint_ratio = (geom["lx"] * geom["ly"]) / max(1.0, pallet_area)
        height_ratio = geom["z1"] / pallet_height
        near_barrier_risk = (1.0 - depth_by_id[node_id]) * (
            0.65 * footprint_ratio + 0.35 * height_ratio
        )
        return (
            0 if node_id in far_targets else 1,
            -depth_band_by_id[node_id],
            -round(depth_by_id[node_id], 9),
            -unlock_far_count,
            -unlock_total_count,
            round(near_barrier_risk, 9),
            round(geom["z0"] / pallet_height, 9),
            original_rank[node_id],
        )

    order, remaining = far_to_near_topological_sequence(
        node_ids,
        predecessors,
        depth_by_id,
        depth_band_by_id,
        score_key,
    )
    has_cycle = bool(remaining)

    for node_id in node_ids:
        item = item_by_id[node_id]
        item["support_predecessors"] = sorted(
            support_predecessors[node_id], key=lambda value: original_rank[value]
        )
        item["clearance_predecessors"] = sorted(
            suction_predecessors[node_id], key=lambda value: original_rank[value]
        )
        item["body_clearance_predecessors"] = sorted(
            body_predecessors[node_id], key=lambda value: original_rank[value]
        )
        item["all_predecessors"] = sorted(
            predecessors[node_id], key=lambda value: original_rank[value]
        )
        item["clearance_successors"] = sorted(
            clearance_successors[node_id], key=lambda value: original_rank[value]
        )
        item["geometric_sequence_feasible"] = not has_cycle
        item["robot_validation_reason"] = (
            "geometry_only_not_verified" if not has_cycle else "dependency_cycle"
        )

    if not has_cycle:
        for sequence, node_id in enumerate(order, start=1):
            item_by_id[node_id]["robot_packing_sequence"] = sequence

    all_edges = {
        (pred, node_id)
        for node_id, preds in predecessors.items()
        for pred in preds
    }
    support_edges = {
        (pred, node_id)
        for node_id, preds in support_predecessors.items()
        for pred in preds
    }
    clearance_edges = {
        (pred, node_id)
        for source in (suction_predecessors, body_predecessors)
        for node_id, preds in source.items()
        for pred in preds
    }
    pallet.update({
        **common_status,
        "suction_rotation_deg": 0,
        "suction_boundary_policy": "overhang_allowed_geometry_only",
        "has_cycle": has_cycle,
        "constraint_count": len(all_edges),
        "support_constraint_count": len(support_edges),
        "clearance_constraint_count": len(clearance_edges),
        "sequence_status": (
            "INFEASIBLE_FIXED_CORNER" if has_cycle else "GEOMETRICALLY_FEASIBLE"
        ),
        "geometric_sequence_feasible": not has_cycle,
        "robot_verified": False,
        "failed_box_ids": list(remaining),
        "cycle_box_ids": list(remaining),
        "duplicate_input_box_ids": duplicate_ids,
    })
    return pallet


def apply_robot_sequences(
    pallets: Iterable[Dict],
    *,
    cup_x: float = SUCTION_CUP_X_MM,
    cup_y: float = SUCTION_CUP_Y_MM,
    robot_reference: str = ROBOT_REFERENCE,
    depth_band_count: int = DEPTH_BAND_COUNT,
) -> Dict:
    """Apply the post-processor to all pallets and return report-level metrics."""
    robot_reference = _validate_robot_reference(robot_reference)
    depth_band_count = max(1, int(depth_band_count))
    stats = {
        "strategy": SEQUENCE_STRATEGY,
        "fixed_corner": FIXED_CORNER,
        "suction_cup_size_mm": [float(cup_x), float(cup_y)],
        "robot_reference": robot_reference,
        "depth_band_count": depth_band_count,
        "depth_policy": DEPTH_POLICY,
        "successful_pallets_processed": 0,
        "geometrically_feasible_pallets": 0,
        "infeasible_pallets": 0,
        "skipped_failed_pallets": 0,
        "sequence_error_pallets": 0,
        "robot_motion_verified": False,
    }
    for pallet in pallets:
        status = str(pallet.get("mpm_status") or "UNKNOWN").upper()
        plan_pallet_robot_sequence(
            pallet,
            cup_x=cup_x,
            cup_y=cup_y,
            robot_reference=robot_reference,
            depth_band_count=depth_band_count,
        )
        sequence_status = str(pallet.get("sequence_status") or "")
        if status == "SUCCESS":
            stats["successful_pallets_processed"] += 1
            if sequence_status == "GEOMETRICALLY_FEASIBLE":
                stats["geometrically_feasible_pallets"] += 1
            elif sequence_status == "INFEASIBLE_FIXED_CORNER":
                stats["infeasible_pallets"] += 1
            else:
                stats["sequence_error_pallets"] += 1
        else:
            stats["skipped_failed_pallets"] += 1
    return stats
