"""Repack low-fill failed pallets as a free box pool."""

from typing import Callable, Dict, List, Optional

from src.geometry.constraint_validator import validate_plan_constraints
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import repack_ready_item


class LowFillRepacker:
    """Higher-budget repack for consecutive low-fill failed pallets."""

    def __init__(
        self,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        validate_center_of_mass: Callable,
        low_fill_threshold: float = 0.18,
        max_attempts: int = 18,
        max_pool_size: int = 80,
        constraint_config=None,
    ):
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._CustomPacker = custom_packer_cls
        self._build_direct_layer = build_direct_layer_solution
        self._validate_com = validate_center_of_mass
        self._low_fill_threshold = low_fill_threshold
        self._max_attempts = max_attempts
        self._max_pool_size = max_pool_size

    def repack(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
        geometric_target_unreachable: bool = False,
    ) -> Dict:
        diag = {
            "low_fill_tried": 0,
            "low_fill_accepted": 0,
            "low_fill_old_pallets": 0,
            "low_fill_new_pallets": 0,
            "low_fill_old_success": 0,
            "low_fill_new_success": 0,
            "low_fill_old_avg_fill": 0.0,
            "low_fill_new_avg_fill": 0.0,
            "reason": "",
        }
        if target_mpm is None or not type_plans:
            diag["reason"] = "no_target_or_empty"
            return diag

        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)

        failed = [
            plan for plan in type_plans
            if plan.get("mpm_status") == "FAILED" and plan.get("packed_items")
        ]
        if len(failed) > 10:
            diag["reason"] = "too_many_failed_pallets_for_low_fill_repack"
            return diag
        low_failed = [
            plan for plan in failed
            if self._fill_rate(plan, pallet_dims) < self._low_fill_threshold
        ]
        total_failed_mpm = sum(float(p.get("mpm_total") or 0) for p in failed)
        if len(low_failed) < 2:
            diag["reason"] = "less_than_2_low_fill_failed"
            return diag
        if total_failed_mpm < target_mpm and not geometric_target_unreachable:
            diag["reason"] = "failed_pool_mpm_below_target"
            return diag

        selected = failed
        old_ids = {
            item.get("id")
            for plan in selected
            for item in plan.get("packed_items", [])
        }
        pool = [
            repack_ready_item(item)
            for plan in selected
            for item in plan.get("packed_items", [])
        ]
        if len(pool) > self._max_pool_size:
            diag["reason"] = "pool_too_large_for_low_fill_repack"
            return diag
        if len(pool) != len(old_ids):
            diag["reason"] = "duplicate_ids_in_selected"
            return diag

        diag["low_fill_tried"] = 1
        diag["low_fill_old_pallets"] = len(selected)
        diag["low_fill_old_success"] = sum(
            1 for p in selected if p.get("mpm_status") == "SUCCESS"
        )
        diag["low_fill_old_avg_fill"] = round(
            sum(self._fill_rate(p, pallet_dims) for p in selected)
            / max(1, len(selected)),
            6,
        )

        rebuilt = self._rebuild_pool(
            pool,
            pallet_dims,
            target_mpm,
            geometric_target_unreachable,
        )
        new_ids = {item.get("id") for packed in rebuilt for item in packed}
        if new_ids != old_ids:
            diag["reason"] = "box_conservation_failed"
            return diag

        new_success = sum(
            1 for packed in rebuilt
            if self._sum_mpm(packed) + 1e-9 >= target_mpm
        )
        new_avg_fill = (
            sum(self._fill_rate({"packed_items": p}, pallet_dims) for p in rebuilt)
            / max(1, len(rebuilt))
        )
        diag["low_fill_new_pallets"] = len(rebuilt)
        diag["low_fill_new_success"] = new_success
        diag["low_fill_new_avg_fill"] = round(new_avg_fill, 6)

        if not self._is_better(diag, geometric_target_unreachable):
            diag["reason"] = "not_better"
            return diag

        selected_ids = {id(p) for p in selected}
        kept = [p for p in type_plans if id(p) not in selected_ids]
        template = selected[0]
        pallet_type = template.get("pallet_type", "UNKNOWN")
        sales_order_no = template.get("sales_order_no", "UNKNOWN_ORDER")
        existing_ids = [p.get("pallet_id") for p in kept]

        def _next_id() -> str:
            idx = len(existing_ids) + 1
            pid = f"{pallet_type}-{sales_order_no}-F{idx}"
            existing_ids.append(pid)
            return pid

        candidate_plans = list(kept)
        for packed in rebuilt:
            if not packed:
                continue
            solution = {
                "pallet_id": _next_id(),
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_target": target_mpm,
                "low_fill_repack": True,
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

        diag["low_fill_accepted"] = 1
        diag["reason"] = "ok"
        return diag

    def _rebuild_pool(
        self,
        pool: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: float,
        geometric_target_unreachable: bool,
    ) -> List[List[Dict]]:
        remaining = list(pool)
        rebuilt: List[List[Dict]] = []
        seed = 88001
        target = None if geometric_target_unreachable else target_mpm
        while remaining:
            packed = []
            if target is not None:
                packed = self._build_direct_layer(
                    remaining,
                    target_mpm=target,
                    pallet_dims=pallet_dims,
                    seed=seed,
                    xy_tolerance=2.0,
                    z_tolerance=0.0,
                    candidate_count=18,
                    constraint_config=self._cfg,
                )
            if not packed:
                packer = self._CustomPacker(
                    pallet_dims,
                    support_ratio_threshold=self._cfg.support_ratio_threshold,
                    size_tolerance=2.0,
                    max_candidate_points=260,
                    max_points_per_layer=80,
                    constraint_config=self._cfg,
                )
                packed, _ = packer.pack(
                    remaining,
                    num_restarts=10 if target is None else 14,
                    beam_width=4,
                    candidate_limit=14,
                    random_seed=seed,
                    target_mpm=target,
                    stop_when_target_met=False,
                    allow_skip_items=True,
                )
            seed += 19
            if not packed:
                break
            rebuilt.append(packed)
            used = {item.get("id") for item in packed}
            new_remaining = [
                item for item in remaining if item.get("id") not in used
            ]
            if len(new_remaining) == len(remaining):
                break
            remaining = new_remaining
            if len(rebuilt) >= self._max_attempts:
                break
        return rebuilt

    def _is_better(
        self,
        diag: Dict,
        geometric_target_unreachable: bool,
    ) -> bool:
        if not geometric_target_unreachable and (
            diag["low_fill_new_success"] > diag["low_fill_old_success"]
        ):
            return True
        if not geometric_target_unreachable:
            return False
        if diag["low_fill_new_pallets"] < diag["low_fill_old_pallets"]:
            return True
        return (
            diag["low_fill_new_pallets"] == diag["low_fill_old_pallets"]
            and diag["low_fill_new_avg_fill"]
            > diag["low_fill_old_avg_fill"] + 0.03
        )

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

    def _sum_mpm(self, items: List[Dict]) -> float:
        return sum(float(item.get("min_pack_multiple", 0) or 0) for item in items)
