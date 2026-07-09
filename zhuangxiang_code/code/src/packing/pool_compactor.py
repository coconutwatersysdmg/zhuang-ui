"""Compact a free box pool into as few valid pallets as possible."""

from copy import deepcopy
from typing import Dict, List, Optional

from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.suction_planner import SuctionPlanner
from src.packing.stacking_policy import (
    build_height_multiple_bonus_by_size,
    sort_same_size_heavier_first,
    stacking_tiebreak_key,
)
from src.utils.helpers import apply_suction_pose_fields, repack_ready_item


class PoolCompactor:
    """Fill-first packer for tail or index-unreachable box pools."""

    def __init__(
        self,
        pallet_dims: Dict[str, float],
        xy_tolerance: float = 2.0,
        z_tolerance: float = 0.0,
        support_ratio_threshold: float = 0.8,
        constraint_config=None,
    ):
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self.pallet_dims = pallet_dims
        self.xy_tolerance = xy_tolerance
        self.z_tolerance = z_tolerance
        self.support_ratio_threshold = constraint_config.support_ratio_threshold
        self.height_multiple_layering_enabled = (
            constraint_config.height_multiple_layering_enabled
        )
        self.reachability_enabled = (
            constraint_config.suction_reachability_enabled
        )
        self.reachability = SuctionPlanner(
            pallet_dims=pallet_dims,
            suction_cup_length=constraint_config.suction_cup_length,
            suction_cup_width=constraint_config.suction_cup_width,
            suction_xy_clearance=constraint_config.suction_xy_clearance,
            suction_z_clearance=constraint_config.suction_z_clearance,
            allow_suction_rotation_90=constraint_config.suction_allow_rotation_90,
        )

    def compact(
        self,
        items: List[Dict],
        max_pallets: int = 3,
    ) -> Dict:
        """Try to place all items using 1..max_pallets pallets."""
        pool = [repack_ready_item(item) for item in items]
        original_ids = {item.get("id") for item in pool}

        best_sets: List[List[Dict]] = []
        best_count = 0
        for target_count in range(1, max_pallets + 1):
            remaining = list(pool)
            packed_sets: List[List[Dict]] = []
            for _ in range(target_count):
                packed = self._pack_one_pallet(remaining)
                if not packed:
                    break
                packed_sets.append(packed)
                used = {item.get("id") for item in packed}
                remaining = [
                    item for item in remaining if item.get("id") not in used
                ]
                if not remaining:
                    break

            packed_ids = {
                item.get("id")
                for packed in packed_sets
                for item in packed
            }
            if len(packed_ids) > best_count:
                best_count = len(packed_ids)
                best_sets = packed_sets
            if packed_ids == original_ids:
                return {
                    "success": True,
                    "packed_sets": packed_sets,
                    "unpacked": [],
                    "attempted_pallets": target_count,
                }

        packed_ids = {
            item.get("id")
            for packed in best_sets
            for item in packed
        }
        unpacked = [item for item in pool if item.get("id") not in packed_ids]
        return {
            "success": False,
            "packed_sets": best_sets,
            "unpacked": unpacked,
            "attempted_pallets": max_pallets,
        }

    def _pack_one_pallet(self, items: List[Dict]) -> List[Dict]:
        columns = self._build_columns(items)
        placed: List[Dict] = []
        x = 0.0
        y = 0.0
        row_width = 0.0

        for column in columns:
            footprint = column["footprint"]
            if x + footprint["length"] > self._pallet_length() + 1e-9:
                x = 0.0
                y += row_width
                row_width = 0.0
            if y + footprint["width"] > self._pallet_width() + 1e-9:
                continue

            placed_column = self._place_column(column["items"], x, y, placed)
            if not placed_column:
                continue

            candidate = placed + placed_column
            if not self._passes_partial_gate(candidate):
                continue

            placed = candidate
            x += footprint["length"]
            row_width = max(row_width, footprint["width"])

        if validate_pallet_constraints(
            {"packed_items": placed}, self.pallet_dims,
            constraint_config=self._cfg,
        )["is_valid"]:
            return placed
        return []

    def _passes_partial_gate(self, items: List[Dict]) -> bool:
        result = validate_pallet_constraints(
            {"packed_items": items},
            self.pallet_dims,
            constraint_config=self._cfg,
        )
        if result["is_valid"]:
            return True
        return all(
            violation.get("type") == "center_of_mass"
            for violation in result.get("violations", [])
        )

    def _build_columns(self, items: List[Dict]) -> List[Dict]:
        remaining = sorted(
            [repack_ready_item(item) for item in items],
            key=lambda item: (
                -self._base_area(item),
                -self._height(item),
                -float(item.get("min_pack_multiple", 0) or 0),
                str(item.get("id")),
            ),
        )
        size_multiple_bonus = (
            build_height_multiple_bonus_by_size(remaining)
            if self.height_multiple_layering_enabled else {}
        )
        remaining.sort(
            key=lambda item: (
                -self._base_area(item),
                stacking_tiebreak_key(item, size_multiple_bonus),
                -float(item.get("min_pack_multiple", 0) or 0),
                str(item.get("id")),
            )
        )
        columns = []
        while remaining:
            bottom = remaining.pop(0)
            column = [bottom]
            height = self._height(bottom)
            support = bottom

            changed = True
            while changed:
                changed = False
                for idx, item in enumerate(sort_same_size_heavier_first(remaining)):
                    if not self._can_stack_on(item, support):
                        continue
                    if height + self._height(item) > self._pallet_height() + 1e-9:
                        continue
                    column.append(item)
                    height += self._height(item)
                    support = item
                    remaining = [
                        candidate for candidate in remaining
                        if candidate.get("id") != item.get("id")
                    ]
                    changed = True
                    break

            columns.append({
                "items": column,
                "footprint": {
                    "length": self._eff_length(bottom),
                    "width": self._eff_width(bottom),
                },
            })

        columns.sort(
            key=lambda col: (
                -len(col["items"]),
                -sum(self._volume(item) for item in col["items"]),
                -col["footprint"]["length"] * col["footprint"]["width"],
            )
        )
        return columns

    def _place_column(
        self,
        column_items: List[Dict],
        x: float,
        y: float,
        placed: List[Dict],
    ) -> Optional[List[Dict]]:
        column: List[Dict] = []
        z = 0.0
        for item in column_items:
            raw = self._raw_dims(item)
            dims = {
                "length": raw["length"] + self.xy_tolerance,
                "width": raw["width"] + self.xy_tolerance,
                "height": raw["height"] + self.z_tolerance,
            }
            point = {"x": x, "y": y, "z": z}
            if self.reachability_enabled:
                suction_pose = self.reachability.find_reachable_suction_pose(
                    point, dims, placed + column, raw_dims=raw
                )
                if suction_pose is None:
                    return None
            else:
                suction_pose = None
            box = deepcopy(item)
            box["position"] = point
            box["length"] = dims["length"]
            box["width"] = dims["width"]
            box["height"] = dims["height"]
            box["raw_length"] = raw["length"]
            box["raw_width"] = raw["width"]
            box["raw_height"] = raw["height"]
            if suction_pose is not None:
                apply_suction_pose_fields(box, suction_pose)
            box["supported_area"] = dims["length"] * dims["width"]
            box["support_ratio"] = 1.0
            column.append(box)
            z += dims["height"]
        return column

    def _can_stack_on(self, item: Dict, support: Dict) -> bool:
        if (
            self._raw_dims(item) == self._raw_dims(support)
            and float(item.get("weight", 0.0) or 0.0)
            > float(support.get("weight", 0.0) or 0.0) + 1e-9
        ):
            return False
        return (
            self._eff_length(item) <= self._eff_length(support) + 1e-9
            and self._eff_width(item) <= self._eff_width(support) + 1e-9
        )

    def _raw_dims(self, item: Dict) -> Dict[str, float]:
        return {
            "length": float(item.get("raw_length", item.get("length", 0)) or 0),
            "width": float(item.get("raw_width", item.get("width", 0)) or 0),
            "height": float(item.get("raw_height", item.get("height", 0)) or 0),
        }

    def _eff_length(self, item: Dict) -> float:
        return self._raw_dims(item)["length"] + self.xy_tolerance

    def _eff_width(self, item: Dict) -> float:
        return self._raw_dims(item)["width"] + self.xy_tolerance

    def _height(self, item: Dict) -> float:
        return self._raw_dims(item)["height"] + self.z_tolerance

    def _base_area(self, item: Dict) -> float:
        raw = self._raw_dims(item)
        return raw["length"] * raw["width"]

    def _volume(self, item: Dict) -> float:
        raw = self._raw_dims(item)
        return raw["length"] * raw["width"] * raw["height"]

    def _pallet_length(self) -> float:
        return float(self.pallet_dims.get("length", 0) or 0)

    def _pallet_width(self) -> float:
        return float(self.pallet_dims.get("width", 0) or 0)

    def _pallet_height(self) -> float:
        return float(self.pallet_dims.get("height", 0) or 0)
