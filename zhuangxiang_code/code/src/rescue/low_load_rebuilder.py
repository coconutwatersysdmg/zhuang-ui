"""低载失败托盘二次合并器。

把少箱数、低指数的 FAILED 托盘合并为自由箱池，尝试重新生成更少的失败盘
或额外 SUCCESS 盘。该模块只处理 FAILED 托盘，接受改动前必须满足箱子守恒。
"""

from typing import Callable, Dict, List, Optional

from src.geometry.constraint_validator import (
    validate_pallet_constraints,
    validate_plan_constraints,
)
from src.packing.pool_compactor import PoolCompactor
from src.packing.suction_planner import SuctionPlanner
from src.rescue.index_builder import IndexBuilder
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import apply_suction_pose_fields, repack_ready_item


class LowLoadRebuilder:
    """低载失败托盘合并重建。"""

    def __init__(
        self,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        validate_center_of_mass: Callable,
        low_box_count: int = 5,
        low_mpm: float = 32.0,
        deep_gap: float = 160.0,
        max_selected_pallets: int = 28,
        max_attempts: int = 8,
        constraint_config=None,
    ):
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._CustomPacker = custom_packer_cls
        self._build_direct_layer = build_direct_layer_solution
        self._validate_com = validate_center_of_mass
        self._low_box_count = low_box_count
        self._low_mpm = low_mpm
        self._deep_gap = deep_gap
        self._max_selected_pallets = max_selected_pallets
        self._max_attempts = max_attempts

    def rebuild(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
    ) -> Dict:
        diag = {
            "low_load_tried": 0,
            "low_load_accepted": 0,
            "low_load_selected_pallets": 0,
            "low_load_new_success": 0,
            "low_load_old_success": 0,
            "low_load_old_pallets": 0,
            "low_load_new_pallets": 0,
            "low_load_box_conservation_ok": True,
            "reason": "",
        }
        if target_mpm is None or not type_plans:
            diag["reason"] = "no_target_or_empty"
            return diag

        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)

        selected = self._select_candidates(type_plans, target_mpm)
        if len(selected) < 2:
            diag["reason"] = "less_than_2_candidates"
            return diag

        diag["low_load_selected_pallets"] = len(selected)
        diag["low_load_old_pallets"] = len(selected)
        diag["low_load_old_success"] = sum(
            1 for plan in selected if plan.get('mpm_status') == 'SUCCESS'
        )
        original_ids = {
            item.get('id')
            for plan in selected
            for item in plan.get('packed_items', [])
        }
        pool = [
            repack_ready_item(item)
            for plan in selected
            for item in plan.get('packed_items', [])
        ]
        if len(pool) != len(original_ids):
            diag["low_load_box_conservation_ok"] = False
            diag["reason"] = "duplicate_ids_in_selected"
            return diag

        diag["low_load_tried"] = 1
        rebuilt_sets = self._rebuild_pool(pool, pallet_dims, target_mpm)
        rebuilt_ids = {
            item.get('id')
            for packed in rebuilt_sets
            for item in packed
        }
        if rebuilt_ids != original_ids:
            diag["low_load_box_conservation_ok"] = False
            diag["reason"] = "box_conservation_failed"
            return diag

        new_success = sum(
            1 for packed in rebuilt_sets
            if self._sum_mpm(packed) + 1e-9 >= target_mpm
        )
        diag["low_load_new_success"] = new_success
        diag["low_load_new_pallets"] = len(rebuilt_sets)

        if not self._is_better(
            old_success=diag["low_load_old_success"],
            new_success=new_success,
            old_pallets=len(selected),
            new_pallets=len(rebuilt_sets),
            old_low_count=self._low_count(selected),
            new_low_count=self._low_count_from_sets(rebuilt_sets, target_mpm),
        ):
            diag["reason"] = "not_better"
            return diag

        selected_ids = {id(plan) for plan in selected}
        kept = [plan for plan in type_plans if id(plan) not in selected_ids]
        template = selected[0]
        pallet_type = template.get('pallet_type', 'UNKNOWN')
        sales_order_no = template.get('sales_order_no', 'UNKNOWN_ORDER')
        existing_ids = [plan.get('pallet_id') for plan in kept]

        def _next_id() -> str:
            idx = len(existing_ids) + 1
            pid = f"{pallet_type}-{sales_order_no}-L{idx}"
            existing_ids.append(pid)
            return pid

        candidate_plans = list(kept)
        for packed in rebuilt_sets:
            if not packed:
                continue
            solution = {
                "pallet_id": _next_id(),
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_target": target_mpm,
                "low_load_rebuild": True,
            }
            PalletEvaluator.calc_pallet_status(solution)
            com = self._validate_com(solution, pallet_dims)
            solution["stability_checks"] = {}
            if com.get("is_stable", False):
                solution["stability_checks"]["status"] = "SUCCESS"
            else:
                solution["stability_checks"]["center_of_mass_failure"] = com
                solution["stability_checks"]["status"] = "FAILED"
            candidate_plans.append(solution)

        gate = validate_plan_constraints(
            candidate_plans, pallet_dims, constraint_config=self._cfg
        )
        if not gate["is_valid"]:
            diag["reason"] = "constraint_gate_failed"
            return diag

        type_plans.clear()
        type_plans.extend(candidate_plans)

        diag["low_load_accepted"] = 1
        diag["reason"] = "ok"
        return diag

    def compact_low_fill_tails(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
        fill_threshold: float = 0.12,
        max_selected_pallets: int = 8,
    ) -> Dict:
        diag = {
            "low_load_tried": 0,
            "low_load_accepted": 0,
            "low_load_selected_pallets": 0,
            "low_load_new_success": 0,
            "low_load_old_success": 0,
            "low_load_old_pallets": 0,
            "low_load_new_pallets": 0,
            "low_load_box_conservation_ok": True,
            "reason": "",
        }
        if target_mpm is None or not type_plans:
            diag["reason"] = "no_target_or_empty"
            return diag

        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)

        selected = [
            plan for plan in type_plans
            if plan.get("mpm_status") == "FAILED"
            and plan.get("packed_items")
            and self._fill_rate(plan, pallet_dims) < fill_threshold
        ][:max_selected_pallets]
        if len(selected) < 2:
            diag["reason"] = "less_than_2_low_fill_tails"
            return diag

        original_ids = {
            item.get("id")
            for plan in selected
            for item in plan.get("packed_items", [])
        }
        pool = [
            repack_ready_item(item)
            for plan in selected
            for item in plan.get("packed_items", [])
        ]
        if len(pool) != len(original_ids):
            diag["low_load_box_conservation_ok"] = False
            diag["reason"] = "duplicate_ids_in_selected"
            return diag

        diag["low_load_tried"] = 1
        diag["low_load_selected_pallets"] = len(selected)
        diag["low_load_old_pallets"] = len(selected)
        diag["low_load_old_success"] = sum(
            1 for plan in selected if plan.get("mpm_status") == "SUCCESS"
        )

        rebuilt_sets = self._pack_leftover_compact(pool, pallet_dims)
        rebuilt_ids = {
            item.get("id")
            for packed in rebuilt_sets
            for item in packed
        }
        if rebuilt_ids != original_ids:
            diag["low_load_box_conservation_ok"] = False
            diag["reason"] = "box_conservation_failed"
            return diag

        diag["low_load_new_success"] = sum(
            1 for packed in rebuilt_sets
            if self._sum_mpm(packed) + 1e-9 >= target_mpm
        )
        diag["low_load_new_pallets"] = len(rebuilt_sets)
        if len(rebuilt_sets) >= len(selected):
            diag["reason"] = "not_fewer_pallets"
            return diag

        selected_ids = {id(plan) for plan in selected}
        kept = [plan for plan in type_plans if id(plan) not in selected_ids]
        template = selected[0]
        pallet_type = template.get("pallet_type", "UNKNOWN")
        sales_order_no = template.get("sales_order_no", "UNKNOWN_ORDER")
        existing_ids = [plan.get("pallet_id") for plan in kept]

        def _next_id() -> str:
            idx = len(existing_ids) + 1
            pid = f"{pallet_type}-{sales_order_no}-LC{idx}"
            existing_ids.append(pid)
            return pid

        candidate_plans = list(kept)
        for packed in rebuilt_sets:
            solution = {
                "pallet_id": _next_id(),
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_target": target_mpm,
                "low_fill_tail_compact": True,
            }
            PalletEvaluator.calc_pallet_status(solution)
            com = self._validate_com(solution, pallet_dims)
            solution["stability_checks"] = {}
            if com.get("is_stable", False):
                solution["stability_checks"]["status"] = "SUCCESS"
            else:
                solution["stability_checks"]["center_of_mass_failure"] = com
                solution["stability_checks"]["status"] = "FAILED"
            candidate_plans.append(solution)

        gate = validate_plan_constraints(
            candidate_plans, pallet_dims, constraint_config=self._cfg
        )
        if not gate["is_valid"]:
            diag["reason"] = "constraint_gate_failed"
            return diag

        type_plans.clear()
        type_plans.extend(candidate_plans)

        diag["low_load_accepted"] = 1
        diag["reason"] = "ok"
        return diag

    def merge_low_load_pairs(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
        max_attempts: int = 16,
    ) -> Dict:
        diag = {
            "low_pair_tried": 0,
            "low_pair_accepted": 0,
            "low_pair_emptied": 0,
            "reason": "",
        }
        if target_mpm is None or not type_plans:
            diag["reason"] = "no_target_or_empty"
            return diag

        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)

        attempts = 0
        while attempts < max_attempts:
            donor = self._next_pair_donor(type_plans)
            if donor is None:
                break
            accepted = False
            for receiver in self._pair_receivers(type_plans, donor):
                attempts += 1
                diag["low_pair_tried"] += 1
                packed = self._try_merge_two_plans(
                    receiver, donor, pallet_dims
                )
                if not packed:
                    if attempts >= max_attempts:
                        break
                    continue
                receiver["packed_items"] = packed
                donor["packed_items"] = []
                PalletEvaluator.calc_pallet_status(receiver)
                PalletEvaluator.calc_pallet_status(donor)
                diag["low_pair_accepted"] += 1
                diag["low_pair_emptied"] += 1
                accepted = True
                break
            donor["_low_pair_blocked"] = not accepted
            if not accepted and attempts >= max_attempts:
                break

        for plan in type_plans:
            plan.pop("_low_pair_blocked", None)
        diag["reason"] = "ok" if diag["low_pair_accepted"] else "no_merge"
        return diag

    def _next_pair_donor(self, type_plans: List[Dict]) -> Optional[Dict]:
        donors = [
            plan for plan in type_plans
            if not plan.get("_low_pair_blocked")
            and plan.get("packed_items")
            and plan.get("mpm_status") == "FAILED"
            and len(plan.get("packed_items", [])) <= 2
        ]
        if not donors:
            return None
        return sorted(
            donors,
            key=lambda plan: (
                len(plan.get("packed_items", [])),
                float(plan.get("mpm_total") or 0.0),
                str(plan.get("pallet_id")),
            ),
        )[0]

    def _pair_receivers(
        self, type_plans: List[Dict], donor: Dict
    ) -> List[Dict]:
        receivers = [
            plan for plan in type_plans
            if plan is not donor
            and plan.get("packed_items")
            and plan.get("mpm_status") == "FAILED"
            and len(plan.get("packed_items", []))
            + len(donor.get("packed_items", [])) <= 24
        ]
        return sorted(
            receivers,
            key=lambda plan: (
                -len(plan.get("packed_items", [])),
                -float(plan.get("mpm_total") or 0.0),
                str(plan.get("pallet_id")),
            ),
        )

    def _try_merge_two_plans(
        self,
        receiver: Dict,
        donor: Dict,
        pallet_dims: Dict[str, float],
    ) -> List[Dict]:
        pool = [
            repack_ready_item(item)
            for plan in (receiver, donor)
            for item in plan.get("packed_items", [])
        ]
        if len(pool) > 24:
            return []
        expected_ids = {item.get("id") for item in pool}
        if not expected_ids or len(expected_ids) != len(pool):
            return []
        packer = self._CustomPacker(
            pallet_dims,
            support_ratio_threshold=self._cfg.support_ratio_threshold,
            size_tolerance=2.0,
            max_candidate_points=260,
            max_points_per_layer=70,
            constraint_config=self._cfg,
        )
        packed, _ = packer.pack(
            pool,
            num_restarts=8,
            beam_width=4,
            candidate_limit=12,
            random_seed=42017,
            target_mpm=None,
            stop_when_target_met=False,
            allow_skip_items=True,
        )
        if {item.get("id") for item in packed} != expected_ids:
            return []
        if not validate_pallet_constraints(
            {"packed_items": packed}, pallet_dims, constraint_config=self._cfg
        )["is_valid"]:
            return []
        return packed

    def _select_candidates(
        self,
        type_plans: List[Dict],
        target_mpm: float,
    ) -> List[Dict]:
        failed = [
            plan for plan in type_plans
            if plan.get('mpm_status') == 'FAILED'
            and plan.get('packed_items')
        ]
        primary = [
            plan for plan in failed
            if len(plan.get('packed_items', [])) <= self._low_box_count
            or float(plan.get('mpm_total') or 0.0) < self._low_mpm
            or float(plan.get('mpm_gap') or 0.0) >= self._deep_gap
        ]
        selected = list(primary)
        selected_ids = {id(plan) for plan in selected}
        min_pool_mpm = max(target_mpm * 1.5, self._low_mpm * 2)
        if self._sum_plan_mpm(selected) < min_pool_mpm:
            for plan in sorted(
                failed,
                key=lambda p: (
                    float(p.get('mpm_total') or 0.0),
                    len(p.get('packed_items', [])),
                    str(p.get('pallet_id')),
                ),
            ):
                if id(plan) in selected_ids:
                    continue
                selected.append(plan)
                selected_ids.add(id(plan))
                if len(selected) >= self._max_selected_pallets:
                    break
                if self._sum_plan_mpm(selected) >= min_pool_mpm:
                    break
        return selected[:self._max_selected_pallets]

    def _rebuild_pool(
        self,
        items: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: float,
    ) -> List[List[Dict]]:
        if len(items) <= 60:
            compact = PoolCompactor(
                pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                constraint_config=self._cfg,
            ).compact(items, max_pallets=3)
            if compact["success"]:
                return compact["packed_sets"]

        pool = list(items)
        rebuilt: List[List[Dict]] = []

        while pool:
            packed = self._pack_floor(pool, pallet_dims, min_count=1)
            if not packed:
                break
            rebuilt.append(packed)
            used = {item.get('id') for item in packed}
            pool = [item for item in pool if item.get('id') not in used]

        return rebuilt

    def _pack_success_candidate(
        self,
        pool: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: float,
        seed: int,
    ) -> List[Dict]:
        for attempt in range(self._max_attempts):
            candidate_pool = IndexBuilder.build_index_bucket_candidate_pool(
                pool,
                target_mpm=target_mpm,
                seed=seed + attempt,
                expand_factor=2.0 + attempt * 0.3,
                pallet_dims=pallet_dims,
            )
            if not candidate_pool:
                continue
            packed = self._build_direct_layer(
                candidate_pool,
                target_mpm=target_mpm,
                pallet_dims=pallet_dims,
                seed=seed + attempt,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                candidate_count=14,
                constraint_config=self._cfg,
            )
            if packed and self._sum_mpm(packed) >= target_mpm:
                return packed

            packer = self._CustomPacker(
                pallet_dims,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                size_tolerance=2.0,
                max_candidate_points=220,
                max_points_per_layer=60,
                constraint_config=self._cfg,
            )
            packed, _ = packer.pack(
                candidate_pool,
                num_restarts=10,
                beam_width=4,
                candidate_limit=12,
                random_seed=seed + attempt,
                target_mpm=target_mpm,
                stop_when_target_met=True,
                allow_skip_items=True,
            )
            if packed and self._sum_mpm(packed) >= target_mpm:
                return packed
        return []

    def _pack_leftover_compact(
        self,
        pool: List[Dict],
        pallet_dims: Dict[str, float],
    ) -> List[List[Dict]]:
        if len(pool) <= 60:
            compact = PoolCompactor(
                pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                constraint_config=self._cfg,
            ).compact(pool, max_pallets=3)
            if compact["success"]:
                return compact["packed_sets"]

        result: List[List[Dict]] = []
        remaining = list(pool)
        seed = 73001
        while remaining:
            packed = self._pack_floor(remaining, pallet_dims)
            if not packed:
                packer = self._CustomPacker(
                    pallet_dims,
                    support_ratio_threshold=self._cfg.support_ratio_threshold,
                    size_tolerance=2.0,
                    max_candidate_points=180,
                    max_points_per_layer=40,
                    constraint_config=self._cfg,
                )
                packed, _ = packer.pack(
                    remaining,
                    num_restarts=6,
                    beam_width=3,
                    candidate_limit=10,
                    random_seed=seed,
                    target_mpm=None,
                    stop_when_target_met=False,
                    allow_skip_items=True,
                )
                seed += 11
            if not packed:
                packed = self._pack_floor(
                    [remaining[0]], pallet_dims, min_count=1
                )
            if not packed:
                break
            result.append(packed)
            used = {item.get('id') for item in packed}
            remaining = [
                item for item in remaining
                if item.get('id') not in used
            ]
        return result

    def _pack_floor(
        self,
        pool: List[Dict],
        pallet_dims: Dict[str, float],
        min_count: int = 2,
    ) -> List[Dict]:
        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        pallet_height = float(pallet_dims.get('height', 0) or 0)
        if pallet_length <= 0 or pallet_width <= 0 or pallet_height <= 0:
            return []

        reachability = SuctionPlanner(
            pallet_dims=pallet_dims,
            suction_cup_length=self._cfg.suction_cup_length,
            suction_cup_width=self._cfg.suction_cup_width,
            suction_xy_clearance=self._cfg.suction_xy_clearance,
            suction_z_clearance=self._cfg.suction_z_clearance,
            allow_suction_rotation_90=self._cfg.suction_allow_rotation_90,
        )
        ordered = sorted(
            pool,
            key=lambda item: (
                -float(item.get('length', 0) or 0)
                * float(item.get('width', 0) or 0),
                -float(item.get('min_pack_multiple', 0) or 0),
                str(item.get('id')),
            ),
        )
        rows: List[List[Dict]] = []
        current_row: List[Dict] = []
        x = 0.0
        y = 0.0
        row_width = 0.0
        for item in ordered:
            raw_dims = {
                "length": float(item.get('length', 0) or 0),
                "width": float(item.get('width', 0) or 0),
                "height": float(item.get('height', 0) or 0),
            }
            dims = {
                "length": raw_dims["length"] + 2.0,
                "width": raw_dims["width"] + 2.0,
                "height": raw_dims["height"],
            }
            if (
                dims["length"] <= 0
                or dims["width"] <= 0
                or dims["height"] <= 0
                or dims["length"] > pallet_length + 1e-9
                or dims["width"] > pallet_width + 1e-9
                or dims["height"] > pallet_height + 1e-9
            ):
                continue
            if x + dims["length"] > pallet_length + 1e-9:
                if current_row:
                    rows.append(current_row)
                current_row = []
                x = 0.0
                y += row_width
                row_width = 0.0
            if y + dims["width"] > pallet_width + 1e-9:
                continue
            point = {"x": x, "y": y, "z": 0.0}
            box = dict(item)
            box["position"] = point
            box["length"] = dims["length"]
            box["width"] = dims["width"]
            box["height"] = dims["height"]
            box["raw_length"] = raw_dims["length"]
            box["raw_width"] = raw_dims["width"]
            box["raw_height"] = raw_dims["height"]
            box["supported_area"] = dims["length"] * dims["width"]
            box["support_ratio"] = 1.0
            current_row.append(box)
            x += dims["length"]
            row_width = max(row_width, dims["width"])
        if current_row:
            rows.append(current_row)

        placed = [item for row in rows for item in row]
        if len(placed) < min_count:
            return []
        if not self._cfg.suction_reachability_enabled:
            return placed
        checked: List[Dict] = []
        for item in placed:
            suction_pose = reachability.find_reachable_suction_pose(
                item["position"],
                {
                    "length": item["length"],
                    "width": item["width"],
                    "height": item["height"],
                },
                checked,
                raw_dims={
                    "length": item["raw_length"],
                    "width": item["raw_width"],
                    "height": item["raw_height"],
                },
            )
            if suction_pose is None:
                return []
            apply_suction_pose_fields(item, suction_pose)
            checked.append(item)
        return checked

    def _center_floor_rows(
        self,
        rows: List[List[Dict]],
        pallet_dims: Dict[str, float],
    ) -> List[Dict]:
        pallet_length = float(pallet_dims.get("length", 0) or 0)
        pallet_width = float(pallet_dims.get("width", 0) or 0)
        row_metrics = []
        total_height = 0.0
        for row in rows:
            row_len = sum(float(item.get("length", 0) or 0) for item in row)
            row_w = max(float(item.get("width", 0) or 0) for item in row)
            row_metrics.append((row_len, row_w))
            total_height += row_w
        y = max(0.0, (pallet_width - total_height) / 2.0)
        placed: List[Dict] = []
        for row, (row_len, row_w) in zip(rows, row_metrics):
            x = max(0.0, (pallet_length - row_len) / 2.0)
            for item in row:
                copy_item = dict(item)
                copy_item["position"] = {"x": x, "y": y, "z": 0.0}
                placed.append(copy_item)
                x += float(copy_item.get("length", 0) or 0)
            y += row_w
        return placed

    def _is_better(
        self,
        old_success: int,
        new_success: int,
        old_pallets: int,
        new_pallets: int,
        old_low_count: int,
        new_low_count: int,
    ) -> bool:
        if new_success > old_success:
            return True
        if new_success == old_success and new_pallets < old_pallets:
            return True
        return False

    def _low_count(self, plans: List[Dict]) -> int:
        return sum(
            1 for plan in plans
            if len(plan.get('packed_items', [])) <= self._low_box_count
            or float(plan.get('mpm_total') or 0.0) < self._low_mpm
            or float(plan.get('mpm_gap') or 0.0) >= self._deep_gap
        )

    def _low_count_from_sets(
        self,
        item_sets: List[List[Dict]],
        target_mpm: float,
    ) -> int:
        count = 0
        for items in item_sets:
            total = self._sum_mpm(items)
            if (
                len(items) <= self._low_box_count
                or total < self._low_mpm
                or target_mpm - total >= self._deep_gap
            ):
                count += 1
        return count

    def _fill_rate(self, plan: Dict, pallet_dims: Dict[str, float]) -> float:
        pallet_volume = (
            float(pallet_dims.get("length", 0) or 0)
            * float(pallet_dims.get("width", 0) or 0)
            * float(pallet_dims.get("height", 0) or 0)
        )
        if pallet_volume <= 0:
            return 0.0
        box_volume = sum(
            float(item.get("length", 0) or 0)
            * float(item.get("width", 0) or 0)
            * float(item.get("height", 0) or 0)
            for item in plan.get("packed_items", [])
        )
        return box_volume / pallet_volume

    def _sum_plan_mpm(self, plans: List[Dict]) -> float:
        return sum(float(plan.get('mpm_total') or 0.0) for plan in plans)

    def _sum_mpm(self, items: List[Dict]) -> float:
        return sum(float(item.get('min_pack_multiple', 0) or 0) for item in items)
