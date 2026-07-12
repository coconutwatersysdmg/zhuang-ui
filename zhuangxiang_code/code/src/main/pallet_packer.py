"""
托盘装箱器

为单个 (托盘类型, 销售订单号) 分组执行装箱循环：
初始装箱 -> 指数优先重试 -> 稳定性验证。
"""

import time
import random
from copy import deepcopy
from typing import Callable, Dict, List, Optional, Tuple

from src.geometry.constraint_validator import validate_pallet_constraints
from src.geometry.support import calculate_direct_supported_area, direct_support_ratio
from src.packing.incremental_gate import incremental_pallet_ok
from src.packing.pool_compactor import PoolCompactor
from src.packing.stacking_policy import (
    build_height_multiple_bonus_by_size,
    passes_same_size_heavier_below_constraint,
    sort_same_size_heavier_first,
    stacking_tiebreak_key,
)
from src.packing.suction_planner import SuctionPlanner
from src.rescue import IndexBuilder, PalletEvaluator
from src.utils.helpers import apply_suction_pose_fields


class PalletPacker:
    """单分组装箱器。

    依赖外部注入的装箱原语（CustomPacker 类、直接整层装箱函数、
    单箱居中装箱函数、重心稳定性验证函数），保持本类对几何细节不可知。
    """

    def __init__(
        self,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        build_centered_single_box_solution: Callable,
        validate_center_of_mass: Callable,
        constraint_config=None,
    ):
        self._CustomPacker = custom_packer_cls
        self._build_direct_layer = build_direct_layer_solution
        self._build_centered_single_box = build_centered_single_box_solution
        self._validate_com = validate_center_of_mass
        # 约束统一配置（单一事实来源）。None 时用与历史一致的默认配置，
        # 保证未接线路径行为不变。
        if constraint_config is None:
            from ..config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._support_ratio = constraint_config.support_ratio_threshold
        self._reachability_enabled = (
            constraint_config.suction_reachability_enabled
        )
        self._same_size_enabled = (
            constraint_config.same_size_heavier_below_enabled
        )

    def pack_group(
        self,
        pallet_type: str,
        sales_order_no: str,
        boxes_in_group: List[Dict],
        target_mpm: Optional[float],
    ) -> Tuple[List[Dict], Dict, Dict]:
        """对单个分组执行完整装箱循环。

        Returns:
            (type_packing_plan, runtime_breakdown, index_diagnostics)
        """
        pallet_dims = boxes_in_group[0]['pallet_dims']
        index_diag = IndexBuilder.build_index_diagnostics(
            boxes_in_group, target_mpm, pallet_dims
        )
        canonical = (index_diag.get("canonical_layer_best") or {}).get("best_mpm")
        geometric_target_unreachable = (
            target_mpm is not None
            and canonical is not None
            and float(canonical) + 1e-9 < float(target_mpm)
        )
        no_theoretical_success = (
            (index_diag.get("theoretical_success_pallets") or 0) <= 0
        )
        total_mpm = sum(
            float(box.get('min_pack_multiple', 0) or 0)
            for box in boxes_in_group
        )
        index_target_unreachable = (
            target_mpm is not None
            and total_mpm + 1e-9 < float(target_mpm)
        )
        index_diag["index_target_unreachable"] = index_target_unreachable
        index_diag["geometric_target_unreachable"] = geometric_target_unreachable

        if index_target_unreachable:
            t0 = time.time()
            type_plan, compact_diag = self._try_pack_unreachable_order(
                boxes_in_group,
                pallet_type,
                sales_order_no,
                pallet_dims,
                target_mpm,
            )
            compact_diag["elapsed_seconds"] = round(time.time() - t0, 4)
            index_diag["unreachable_compact"] = compact_diag
            if compact_diag.get("accepted"):
                return (
                    type_plan,
                    {"packing": time.time() - t0, "retry": 0.0},
                    index_diag,
                )

        # canonical_layer_best is only a diagnostic lower-bound heuristic for
        # pure layered packing. Mixed layers can still reach the real target,
        # so do not let the geometric marker disable target pursuit when the
        # order has enough total MPM.
        pack_target_mpm = target_mpm
        fill_aware_main = True

        unfitted = list(boxes_in_group)
        type_plan: List[Dict] = []
        pallet_counter = 1
        no_progress_rounds = 0
        pack_time = 0.0
        retry_time = 0.0
        topup_time = 0.0
        topup_diag = {
            "main_topup_attempts": 0,
            "main_topup_accepted": 0,
            "main_topup_rejected_future_success": 0,
            "main_topup_rejected_constraints": 0,
        }
        hard_recipe_diag = {
            "hard_recipe_attempts": 0,
            "hard_recipe_candidates": 0,
            "hard_recipe_selected": 0,
        }

        while unfitted:
            pallet_id = f"{pallet_type}-{sales_order_no}-{pallet_counter}"

            t0 = time.time()
            packed = self._initial_pack(
                unfitted, pack_target_mpm, pallet_dims, pallet_counter,
                fill_aware=fill_aware_main,
                hard_recipe_diag=hard_recipe_diag,
            )
            pack_time += (time.time() - t0)

            packed_ids = {b['id'] for b in packed}
            remaining = [b for b in unfitted if b['id'] not in packed_ids]

            base_score, base_total, _, _ = PalletEvaluator.evaluate_pallet_solution(
                packed, remaining, target_mpm
            )
            best = {
                "packed_items": packed,
                "remaining_unfitted": remaining,
                "score": base_score,
                "total_mpm": base_total,
            }

            if (
                target_mpm is not None
                and pack_target_mpm is not None
                and not geometric_target_unreachable
                and base_total < target_mpm
                and remaining
            ):
                t1 = time.time()
                best = self._retry_for_index(
                    best, unfitted, target_mpm, pallet_dims, pallet_counter,
                    fill_aware=fill_aware_main,
                )
                retry_time += (time.time() - t1)

            packed = best["packed_items"]
            remaining = best["remaining_unfitted"]

            if packed and remaining:
                t_topup = time.time()
                packed, remaining = self._top_up_current_pallet_to_fill(
                    packed,
                    remaining,
                    pallet_dims,
                    target_mpm,
                    seed=pallet_counter * 7919,
                    diag=topup_diag,
                )
                topup_time += time.time() - t_topup
                best["packed_items"] = packed
                best["remaining_unfitted"] = remaining
                best["total_mpm"] = sum(
                    float(box.get('min_pack_multiple', 0) or 0)
                    for box in packed
                )

            if not packed:
                print(
                    f"  - 托盘 {pallet_id} 未找到可行装箱方案，"
                    f"启用剩余箱子守恒兜底。"
                )
                pallet_counter = self._append_conservation_fallback_pallets(
                    type_plan,
                    unfitted,
                    pallet_type,
                    sales_order_no,
                    pallet_dims,
                    target_mpm,
                    pallet_counter,
                )
                unfitted = []
                break

            if len(remaining) == len(unfitted):
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0

            if no_progress_rounds >= 2:
                print(
                    f"  - 托盘 {pallet_id} 检测到连续无进展，"
                    f"启用剩余箱子守恒兜底。"
                )
                pallet_counter = self._append_conservation_fallback_pallets(
                    type_plan,
                    unfitted,
                    pallet_type,
                    sales_order_no,
                    pallet_dims,
                    target_mpm,
                    pallet_counter,
                )
                unfitted = []
                break

            total_mpm = best["total_mpm"]
            mpm_gap = None if target_mpm is None else (target_mpm - total_mpm)
            mpm_status = (
                "UNKNOWN" if target_mpm is None
                else ("SUCCESS" if total_mpm >= target_mpm else "FAILED")
            )

            solution = {
                "pallet_id": pallet_id,
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_total": total_mpm,
                "mpm_target": target_mpm,
                "mpm_gap": mpm_gap,
                "mpm_status": mpm_status,
            }

            gate = validate_pallet_constraints(
                solution, pallet_dims, constraint_config=self._cfg
            )
            if not gate["is_valid"]:
                raise RuntimeError(
                    "装箱阶段生成了违反约束的托盘，已拒绝输出："
                    f"pallet_id={solution['pallet_id']}, "
                    f"violations={gate['violations'][:5]}"
                )

            com = self._validate_com(solution, pallet_dims)
            solution['stability_checks'] = {}
            details = []
            if not com['is_stable']:
                solution['stability_checks']['center_of_mass_failure'] = com
                details.append("整体重心偏移")
            solution['stability_checks']['status'] = (
                "SUCCESS" if not details else "FAILED"
            )

            type_plan.append(solution)
            unfitted = remaining
            pallet_counter += 1

        t_tail = time.time()
        main_tail_diag = self._absorb_tail_fragments_in_main(
            type_plan, pallet_dims, target_mpm
        )
        pack_time += time.time() - t_tail
        if main_tail_diag.get("tail_absorb_donor_emptied", 0):
            type_plan[:] = [
                plan for plan in type_plan
                if plan.get("packed_items")
            ]
        index_diag["main_tail_absorb"] = main_tail_diag
        index_diag["main_topup"] = {
            **topup_diag,
            "main_topup_seconds": round(topup_time, 4),
        }
        index_diag["main_hard_recipe"] = {
            key: value
            for key, value in hard_recipe_diag.items()
            if not key.startswith("_")
        }

        runtime = {"packing": pack_time, "retry": retry_time, "topup": topup_time}
        return type_plan, runtime, index_diag

    def _absorb_tail_fragments_in_main(
        self,
        type_plan: List[Dict],
        pallet_dims: Dict,
        target_mpm: Optional[float],
    ) -> Dict:
        if target_mpm is None or len(type_plan) < 2:
            return {}
        low_count = sum(
            1 for plan in type_plan
            if plan.get("packed_items")
            and plan.get("mpm_status") == "FAILED"
            and (
                len(plan.get("packed_items", [])) <= 5
                or float(plan.get("mpm_total") or 0.0) < 32.0
                or float(plan.get("mpm_gap") or 0.0) > 160.0
            )
        )
        if low_count < 2:
            return {}
        from src.rescue.tail_fragment_absorber import TailFragmentAbsorber

        return TailFragmentAbsorber(
            low_box_count=5,
            low_mpm=32.0,
            deep_gap=160.0,
            max_attempts=40,
            max_items_per_donor=4,
            seed_base=74000,
        ).absorb(type_plan, pallet_dims, target_mpm)

    def _try_pack_unreachable_order(
        self,
        boxes_in_group: List[Dict],
        pallet_type: str,
        sales_order_no: str,
        pallet_dims: Dict,
        target_mpm: Optional[float],
    ) -> Tuple[List[Dict], Dict]:
        compactor = PoolCompactor(
            pallet_dims,
            xy_tolerance=2.0,
            z_tolerance=0.0,
            support_ratio_threshold=self._support_ratio,
            constraint_config=self._cfg,
        )
        result = compactor.compact(boxes_in_group, max_pallets=3)
        packed_sets = result["packed_sets"] if result["success"] else []
        remaining_after_fill: List[Dict] = []
        if not packed_sets:
            packed_sets, remaining_after_fill = self._pack_fill_first_pool(
                boxes_in_group, pallet_dims
            )
            packed_ids = {
                item.get('id')
                for packed in packed_sets
                for item in packed
            }
            remaining_after_fill = [
                item for item in boxes_in_group
                if item.get('id') not in packed_ids
            ]
            if not self._is_acceptable_unreachable_fill_plan(
                packed_sets, remaining_after_fill, boxes_in_group, pallet_dims
            ):
                return [], {
                    "mode": "index_unreachable_tail",
                    "accepted": False,
                    "success": False,
                    "attempted_pallets": result["attempted_pallets"],
                    "compact_pallets": len(result["packed_sets"]),
                    "fill_first_pallets": len(packed_sets),
                    "unpacked_after_compact": len(remaining_after_fill),
                    "reason": "fill_first_not_compact_enough",
                }

        type_plan: List[Dict] = []
        pallet_counter = 1
        for packed in packed_sets:
            self._append_solution(
                type_plan,
                packed,
                pallet_type,
                sales_order_no,
                pallet_dims,
                target_mpm,
                pallet_counter,
                conservation_fallback=True,
            )
            pallet_counter += 1

        if remaining_after_fill:
            pallet_counter = self._append_conservation_fallback_pallets(
                type_plan,
                remaining_after_fill,
                pallet_type,
                sales_order_no,
                pallet_dims,
                target_mpm,
                pallet_counter,
            )

        packed_ids = {
            item.get('id')
            for plan in type_plan
            for item in plan.get('packed_items', [])
        }
        expected_ids = {item.get('id') for item in boxes_in_group}
        if packed_ids != expected_ids:
            return [], {
                "mode": "index_unreachable_tail",
                "accepted": False,
                "success": False,
                "attempted_pallets": result["attempted_pallets"],
                "compact_pallets": len(result["packed_sets"]),
                "fill_first_pallets": len(packed_sets),
                "unpacked_after_compact": len(expected_ids - packed_ids),
                "reason": "box_conservation_failed",
            }

        return type_plan, {
            "mode": "index_unreachable_tail",
            "accepted": True,
            "success": True,
            "attempted_pallets": result["attempted_pallets"],
            "compact_pallets": len(result["packed_sets"]) if result["success"] else 0,
            "fill_first_pallets": 0 if result["success"] else len(packed_sets),
            "unpacked_after_compact": 0,
            "reason": "compact_ok" if result["success"] else "fill_first_ok",
        }

    def _is_acceptable_unreachable_fill_plan(
        self,
        packed_sets: List[List[Dict]],
        remaining: List[Dict],
        all_items: List[Dict],
        pallet_dims: Dict,
    ) -> bool:
        if remaining or not packed_sets:
            return False
        pallet_volume = (
            float(pallet_dims.get('length', 0) or 0)
            * float(pallet_dims.get('width', 0) or 0)
            * float(pallet_dims.get('height', 0) or 0)
        )
        if pallet_volume <= 0:
            return False
        total_volume = sum(
            float(item.get('length', 0) or 0)
            * float(item.get('width', 0) or 0)
            * float(item.get('height', 0) or 0)
            for item in all_items
        )
        volume_lower_bound = max(1, int((total_volume + pallet_volume - 1) // pallet_volume))
        max_reasonable_pallets = max(4, int(volume_lower_bound * 1.6) + 1)
        if len(packed_sets) > max_reasonable_pallets:
            return False
        avg_fill = total_volume / (pallet_volume * len(packed_sets))
        return avg_fill >= 0.18 or len(packed_sets) <= volume_lower_bound + 1

    def _pack_fill_first_pool(
        self,
        items: List[Dict],
        pallet_dims: Dict,
    ) -> Tuple[List[List[Dict]], List[Dict]]:
        remaining = list(items)
        packed_sets: List[List[Dict]] = []
        seed = 61001
        no_progress = 0
        while remaining and no_progress < 2:
            candidate_pool = self._build_fill_candidate_pool(
                remaining, pallet_dims, seed, max_items=96
            )
            packer = self._CustomPacker(
                pallet_dims,
                support_ratio_threshold=self._support_ratio,
                size_tolerance=2.0,
                max_candidate_points=240,
                max_points_per_layer=70,
                constraint_config=self._cfg,
            )
            packed, _ = packer.pack(
                candidate_pool,
                num_restarts=8,
                beam_width=3,
                candidate_limit=12,
                random_seed=seed,
                target_mpm=None,
                stop_when_target_met=False,
                allow_skip_items=True,
            )
            seed += 17
            if packed and not self._is_valid_packed(packed, pallet_dims):
                packed = []
            if not packed:
                packed = self._pack_residual_floor(remaining, pallet_dims)
                if packed and not self._is_valid_packed(packed, pallet_dims):
                    packed = []
            if not packed:
                no_progress += 1
                continue
            used = {item.get('id') for item in packed}
            new_remaining = [
                item for item in remaining if item.get('id') not in used
            ]
            if len(new_remaining) == len(remaining):
                no_progress += 1
                continue
            packed_sets.append(packed)
            remaining = new_remaining
            no_progress = 0
        return packed_sets, remaining

    def _append_conservation_fallback_pallets(
        self,
        type_plan: List[Dict],
        remaining_items: List[Dict],
        pallet_type: str,
        sales_order_no: str,
        pallet_dims: Dict,
        target_mpm: Optional[float],
        pallet_counter: int,
    ) -> int:
        """把剩余箱子重新打包，保证输出不漏箱且尽量避免单箱托盘。"""
        pool = list(remaining_items)
        no_progress = 0
        batch_seed = pallet_counter * 1009

        while pool and no_progress < 2:
            packed = self._pack_residual_pool(pool, pallet_dims, batch_seed)
            batch_seed += 17
            if not packed:
                no_progress += 1
                continue

            packed_ids = {box['id'] for box in packed}
            if not packed_ids:
                no_progress += 1
                continue

            self._append_solution(
                type_plan,
                packed,
                pallet_type,
                sales_order_no,
                pallet_dims,
                target_mpm,
                pallet_counter,
                conservation_fallback=True,
            )
            pallet_counter += 1
            new_pool = [box for box in pool if box['id'] not in packed_ids]
            no_progress = 0 if len(new_pool) < len(pool) else no_progress + 1
            pool = new_pool

        for item in pool:
            packed = self._build_centered_single_box(
                [item],
                pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._support_ratio,
                constraint_config=self._cfg,
            )
            if not packed:
                raise RuntimeError(
                    "剩余箱子无法合法放入单箱兜底托盘，"
                    f"box_id={item.get('id')}, "
                    f"box_type={item.get('type')}, "
                    f"pallet_type={pallet_type}, "
                    f"sales_order_no={sales_order_no}"
                )

            self._append_solution(
                type_plan,
                packed,
                pallet_type,
                sales_order_no,
                pallet_dims,
                target_mpm,
                pallet_counter,
                conservation_fallback=True,
                single_box_fallback=True,
            )
            pallet_counter += 1

        return pallet_counter

    def _pack_residual_pool(
        self,
        pool: List[Dict],
        pallet_dims: Dict,
        seed: int,
    ) -> List[Dict]:
        """残余池按装箱量优先重装，不强求达到指数目标。"""
        candidate_pool = self._build_fill_candidate_pool(
            pool, pallet_dims, seed, max_items=56
        )
        packer = self._CustomPacker(
            pallet_dims,
            support_ratio_threshold=self._support_ratio,
            size_tolerance=2.0,
            max_candidate_points=120,
            max_points_per_layer=35,
            constraint_config=self._cfg,
        )
        packed, _ = packer.pack(
            candidate_pool,
            num_restarts=3,
            beam_width=2,
            candidate_limit=8,
            random_seed=seed,
            target_mpm=None,
            stop_when_target_met=False,
            allow_skip_items=True,
        )
        if packed and validate_pallet_constraints(
            {"packed_items": packed}, pallet_dims, constraint_config=self._cfg
        )["is_valid"]:
            return packed

        floor_packed = self._pack_residual_floor(pool, pallet_dims)
        if floor_packed and validate_pallet_constraints(
            {"packed_items": floor_packed}, pallet_dims,
            constraint_config=self._cfg,
        )["is_valid"]:
            return floor_packed

        return []

    def _build_fill_candidate_pool(
        self,
        items: List[Dict],
        pallet_dims: Dict,
        seed: int,
        max_items: int = 64,
    ) -> List[Dict]:
        """Build a bounded pool for fill-first packing when no MPM target is used."""
        if len(items) <= max_items:
            return list(items)

        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        pallet_height = float(pallet_dims.get('height', 0) or 0)
        groups: Dict[Tuple[float, float, float, float], List[Dict]] = {}

        for item in items:
            key = (
                float(item.get('length', 0) or 0),
                float(item.get('width', 0) or 0),
                float(item.get('height', 0) or 0),
                float(item.get('min_pack_multiple', 0) or 0),
            )
            groups.setdefault(key, []).append(item)

        def _group_score(entry):
            (length, width, height, mpm), group = entry
            eff_l = length + 2.0
            eff_w = width + 2.0
            eff_h = height
            if (
                eff_l <= 0
                or eff_w <= 0
                or eff_h <= 0
                or pallet_length <= 0
                or pallet_width <= 0
                or pallet_height <= 0
            ):
                return (0.0, 0, 0, 0.0)
            per_layer = int(pallet_length // eff_l) * int(pallet_width // eff_w)
            layers = int(pallet_height // eff_h)
            max_fit = max(0, per_layer * layers)
            usable = min(len(group), max_fit if max_fit > 0 else len(group))
            volume = length * width * height
            return (usable * volume, usable, per_layer, mpm)

        ordered_groups = sorted(
            groups.items(),
            key=lambda entry: (*_group_score(entry), str(entry[1][0].get('type'))),
            reverse=True,
        )

        selected: List[Dict] = []
        seen = set()
        for entry in ordered_groups:
            score_volume, _, _, _ = _group_score(entry)
            if score_volume <= 0:
                continue
            group = sorted(entry[1], key=lambda b: str(b.get('id')))
            for item in group:
                if item['id'] in seen:
                    continue
                selected.append(item)
                seen.add(item['id'])
                if len(selected) >= max_items:
                    return selected

        if len(selected) < max_items:
            import random
            rng = random.Random(seed)
            remainder = [item for item in items if item.get('id') not in seen]
            remainder.sort(
                key=lambda item: (
                    -float(item.get('length', 0) or 0)
                    * float(item.get('width', 0) or 0)
                    * float(item.get('height', 0) or 0),
                    str(item.get('id')),
                )
            )
            if seed % 2:
                rng.shuffle(remainder)
            selected.extend(remainder[:max_items - len(selected)])

        return selected

    def _pack_residual_floor(
        self,
        pool: List[Dict],
        pallet_dims: Dict,
    ) -> List[Dict]:
        """残余箱保守底层装箱：不堆叠，只合并能平铺的尾箱。"""
        if not pool:
            return []

        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        if pallet_length <= 0 or pallet_width <= 0:
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
                'length': float(item.get('length', 0) or 0),
                'width': float(item.get('width', 0) or 0),
                'height': float(item.get('height', 0) or 0),
            }
            dims = {
                'length': raw_dims['length'] + 2.0,
                'width': raw_dims['width'] + 2.0,
                'height': raw_dims['height'],
            }
            if (
                dims['length'] <= 0
                or dims['width'] <= 0
                or dims['height'] <= 0
                or dims['length'] > pallet_length + 1e-9
                or dims['width'] > pallet_width + 1e-9
                or dims['height'] > float(pallet_dims.get('height', 0) or 0) + 1e-9
            ):
                continue
            if x + dims['length'] > pallet_length + 1e-9:
                if current_row:
                    rows.append(current_row)
                current_row = []
                x = 0.0
                y += row_width
                row_width = 0.0
            if y + dims['width'] > pallet_width + 1e-9:
                continue

            point = {'x': x, 'y': y, 'z': 0.0}

            item_copy = deepcopy(item)
            item_copy['position'] = point
            item_copy['length'] = dims['length']
            item_copy['width'] = dims['width']
            item_copy['height'] = dims['height']
            item_copy['raw_length'] = raw_dims['length']
            item_copy['raw_width'] = raw_dims['width']
            item_copy['raw_height'] = raw_dims['height']
            item_copy['supported_area'] = dims['length'] * dims['width']
            item_copy['support_ratio'] = 1.0
            current_row.append(item_copy)
            x += dims['length']
            row_width = max(row_width, dims['width'])

        if current_row:
            rows.append(current_row)

        placed = [item for row in rows for item in row]
        if len(placed) < 2:
            return []

        if not self._reachability_enabled:
            return placed

        checked: List[Dict] = []
        for item in placed:
            suction_pose = reachability.find_reachable_suction_pose(
                item['position'],
                {
                    'length': item['length'],
                    'width': item['width'],
                    'height': item['height'],
                },
                checked,
                raw_dims={
                    'length': item['raw_length'],
                    'width': item['raw_width'],
                    'height': item['raw_height'],
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
        pallet_dims: Dict,
    ) -> List[Dict]:
        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        row_metrics = []
        total_height = 0.0
        for row in rows:
            row_len = sum(float(item.get('length', 0) or 0) for item in row)
            row_w = max(float(item.get('width', 0) or 0) for item in row)
            row_metrics.append((row_len, row_w))
            total_height += row_w
        y = max(0.0, (pallet_width - total_height) / 2.0)
        placed: List[Dict] = []
        for row, (row_len, row_w) in zip(rows, row_metrics):
            x = max(0.0, (pallet_length - row_len) / 2.0)
            for item in row:
                copy_item = deepcopy(item)
                copy_item['position'] = {'x': x, 'y': y, 'z': 0.0}
                placed.append(copy_item)
                x += float(copy_item.get('length', 0) or 0)
            y += row_w
        return placed

    def _append_solution(
        self,
        type_plan: List[Dict],
        packed: List[Dict],
        pallet_type: str,
        sales_order_no: str,
        pallet_dims: Dict,
        target_mpm: Optional[float],
        pallet_counter: int,
        conservation_fallback: bool = False,
        single_box_fallback: bool = False,
    ) -> None:
        total_mpm = sum(
            float(box.get('min_pack_multiple', 0) or 0)
            for box in packed
        )
        mpm_gap = None if target_mpm is None else (target_mpm - total_mpm)
        mpm_status = (
            "UNKNOWN" if target_mpm is None
            else ("SUCCESS" if total_mpm >= target_mpm else "FAILED")
        )
        solution = {
            "pallet_id": f"{pallet_type}-{sales_order_no}-{pallet_counter}",
            "pallet_type": pallet_type,
            "sales_order_no": sales_order_no,
            "packed_items": packed,
            "mpm_total": total_mpm,
            "mpm_target": target_mpm,
            "mpm_gap": mpm_gap,
            "mpm_status": mpm_status,
        }
        if conservation_fallback:
            solution["conservation_fallback"] = True
        if single_box_fallback:
            solution["single_box_fallback"] = True

        gate = validate_pallet_constraints(
            solution, pallet_dims, constraint_config=self._cfg
        )
        if not gate["is_valid"]:
            raise RuntimeError(
                "装箱阶段生成了违反约束的托盘，已拒绝输出："
                f"pallet_id={solution['pallet_id']}, "
                f"violations={gate['violations'][:5]}"
            )

        com = self._validate_com(solution, pallet_dims)
        solution['stability_checks'] = {}
        if not com['is_stable']:
            solution['stability_checks']['center_of_mass_failure'] = com
            solution['stability_checks']['status'] = "FAILED"
        else:
            solution['stability_checks']['status'] = "SUCCESS"

        type_plan.append(solution)

    def _initial_pack(
        self,
        unfitted: List[Dict],
        target_mpm: Optional[float],
        pallet_dims: Dict,
        pallet_counter: int,
        fill_aware: bool = True,
        hard_recipe_diag: Optional[Dict] = None,
    ) -> List[Dict]:
        """初始装箱：直接整层 -> CustomPacker -> 单箱居中（少量箱时）。"""
        candidates: List[List[Dict]] = []
        preferred_topup: List[Dict] = []
        if target_mpm is not None:
            packed = self._build_hard_recipe_candidate(
                unfitted,
                target_mpm=target_mpm,
                pallet_dims=pallet_dims,
                seed=pallet_counter * 313,
                diag=hard_recipe_diag,
            )
            if packed and self._is_valid_packed(packed, pallet_dims):
                candidates.append(packed)
            packed = self._build_direct_layer(
                unfitted,
                target_mpm=target_mpm,
                pallet_dims=pallet_dims,
                seed=pallet_counter,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                candidate_count=12,
                prefer_fill=fill_aware,
                constraint_config=self._cfg,
            )
            if packed and self._is_valid_packed(packed, pallet_dims):
                if not fill_aware:
                    return packed
                candidates.append(packed)
            packed = self._build_base_with_topup_candidate(
                unfitted, target_mpm, pallet_dims
            )
            if packed and self._is_valid_packed(packed, pallet_dims):
                preferred_topup = packed
                candidates.append(packed)

        fill_first = target_mpm is None
        packer = self._CustomPacker(
            pallet_dims,
            support_ratio_threshold=self._support_ratio,
            size_tolerance=2.0,
            max_candidate_points=100 if fill_first else 120,
            max_points_per_layer=30 if fill_first else 25,
            constraint_config=self._cfg,
        )
        if fill_first:
            pool = self._build_fill_candidate_pool(
                unfitted, pallet_dims, pallet_counter, max_items=56
            )
        elif fill_aware:
            pool = IndexBuilder.build_pallet_candidate_pool(
                unfitted,
                target_mpm=target_mpm,
                seed=pallet_counter,
                pallet_dims=pallet_dims,
                max_items=96,
            )
        else:
            pool = IndexBuilder.build_index_bucket_candidate_pool(
                unfitted,
                target_mpm=target_mpm,
                seed=pallet_counter,
                expand_factor=1.0,
                pallet_dims=pallet_dims,
            )
        packed, _ = packer.pack(
            pool,
            num_restarts=2 if fill_first else 4,
            beam_width=1 if fill_first else 2,
            candidate_limit=6 if fill_first else 7,
            random_seed=None,
            target_mpm=target_mpm,
            stop_when_target_met=False if fill_aware else True,
            allow_skip_items=not fill_first,
        )
        if packed and self._is_valid_packed(packed, pallet_dims):
            candidates.append(packed)

        if len(unfitted) <= 4:
            packed = self._build_centered_single_box(
                unfitted,
                pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._support_ratio,
                constraint_config=self._cfg,
            )
            if packed and self._is_valid_packed(packed, pallet_dims):
                candidates.append(packed)
        selected = self._select_best_packed_candidate(
            candidates, unfitted, target_mpm
        )
        if selected and hard_recipe_diag is not None:
            selected_ids = {item.get('id') for item in selected}
            hard_ids = hard_recipe_diag.get("_last_hard_candidate_ids")
            if hard_ids and selected_ids == hard_ids:
                hard_recipe_diag["hard_recipe_selected"] = (
                    hard_recipe_diag.get("hard_recipe_selected", 0) + 1
                )
        if (
            preferred_topup
            and target_mpm is not None
            and sum(float(item.get('min_pack_multiple', 0) or 0) for item in preferred_topup) >= target_mpm
            and self._packed_fill_rate(preferred_topup, pallet_dims) >= 0.85
            and self._success_potential(
                preferred_topup,
                [
                    item for item in unfitted
                    if item.get('id') not in {
                        packed_item.get('id')
                        for packed_item in preferred_topup
                    }
                ],
                target_mpm,
            )
            >= self._success_potential(
                selected,
                [
                    item for item in unfitted
                    if item.get('id') not in {
                        packed_item.get('id')
                        for packed_item in selected
                    }
                ],
                target_mpm,
            )
            and (
                not selected
                or self._packed_fill_rate(selected, pallet_dims)
                <= self._packed_fill_rate(preferred_topup, pallet_dims) + 0.03
            )
        ):
            return preferred_topup
        return selected

    def _build_hard_recipe_candidate(
        self,
        items: List[Dict],
        target_mpm: float,
        pallet_dims: Dict,
        seed: int = 0,
        diag: Optional[Dict] = None,
    ) -> List[Dict]:
        """Build a target pallet that consumes boxes unable to reach target alone.

        This is a main-flow candidate, not a rescue step. It gives hard boxes a
        chance to reserve compatible fillers before easy homogeneous recipes
        consume them, while final candidate selection still protects global
        success potential.
        """
        hard_groups = self._rank_hard_groups(items, target_mpm, pallet_dims)
        if not hard_groups:
            return []

        best: List[Dict] = []
        best_key = None
        source_by_id = {item.get('id'): item for item in items}
        for group_info in hard_groups[:6]:
            if diag is not None:
                diag["hard_recipe_attempts"] = (
                    diag.get("hard_recipe_attempts", 0) + 1
                )
            hard_ids = set(group_info["ids"])
            hard_items = [
                source_by_id[item_id]
                for item_id in hard_ids
                if item_id in source_by_id
            ]
            if not hard_items:
                continue
            fillers = [
                item for item in items
                if item.get('id') not in hard_ids
            ]
            fillers.sort(
                key=lambda item: (
                    float(item.get('height', 0) or 0),
                    float(item.get('min_pack_multiple', 0) or 0),
                    -float(item.get('length', 0) or 0)
                    * float(item.get('width', 0) or 0),
                    str(item.get('id')),
                )
            )
            pool = hard_items + fillers[:160]
            packed = self._build_direct_layer(
                pool,
                target_mpm=target_mpm,
                pallet_dims=pallet_dims,
                seed=seed,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                candidate_count=16,
                prefer_fill=True,
                constraint_config=self._cfg,
            )
            if not packed:
                continue
            packed_ids = {item.get('id') for item in packed}
            hard_used = len(packed_ids & hard_ids)
            if hard_used <= 0:
                continue
            total_mpm = sum(
                float(item.get('min_pack_multiple', 0) or 0)
                for item in packed
            )
            if total_mpm + 1e-9 < target_mpm:
                continue
            if diag is not None:
                diag["hard_recipe_candidates"] = (
                    diag.get("hard_recipe_candidates", 0) + 1
                )
            fill = self._packed_fill_rate(packed, pallet_dims)
            overflow = max(0.0, total_mpm - target_mpm)
            key = (hard_used, fill, -overflow, len(packed))
            if best_key is None or key > best_key:
                best_key = key
                best = packed
        if best and diag is not None:
            diag["_last_hard_candidate_ids"] = {
                item.get('id') for item in best
            }
        return best

    def _rank_hard_groups(
        self,
        items: List[Dict],
        target_mpm: float,
        pallet_dims: Dict,
    ) -> List[Dict]:
        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        pallet_height = float(pallet_dims.get('height', 0) or 0)
        if pallet_length <= 0 or pallet_width <= 0 or pallet_height <= 0:
            return []

        groups: Dict[Tuple[float, float, float, float], List[Dict]] = {}
        for item in items:
            mpm = float(item.get('min_pack_multiple', 0) or 0)
            length = float(item.get('length', 0) or 0)
            width = float(item.get('width', 0) or 0)
            height = float(item.get('height', 0) or 0)
            if mpm <= 0 or length <= 0 or width <= 0 or height <= 0:
                continue
            groups.setdefault((length, width, height, mpm), []).append(item)

        ranked = []
        for (length, width, height, mpm), group in groups.items():
            eff_l = length + 2.0
            eff_w = width + 2.0
            eff_h = height
            per_layer = int(pallet_length // eff_l) * int(pallet_width // eff_w)
            max_layers = int(pallet_height // eff_h)
            max_count = min(len(group), max(0, per_layer * max_layers))
            if max_count <= 0:
                continue
            single_type_capacity = max_count * mpm
            if single_type_capacity + 1e-9 >= target_mpm:
                continue
            deficit = target_mpm - single_type_capacity
            ranked.append({
                "ids": [
                    item.get('id')
                    for item in sorted(group, key=lambda box: str(box.get('id')))[:max_count]
                ],
                "deficit": deficit,
                "capacity": single_type_capacity,
                "count": max_count,
                "mpm": mpm,
                "volume": length * width * height,
            })

        ranked.sort(
            key=lambda info: (
                info["deficit"],
                -info["capacity"],
                -info["mpm"],
                -info["volume"],
            )
        )
        return ranked

    def _packed_fill_rate(self, packed: List[Dict], pallet_dims: Dict) -> float:
        pallet_volume = (
            float(pallet_dims.get('length', 0) or 0)
            * float(pallet_dims.get('width', 0) or 0)
            * float(pallet_dims.get('height', 0) or 0)
        )
        if pallet_volume <= 0:
            return 0.0
        return sum(
            float(item.get('length', 0) or 0)
            * float(item.get('width', 0) or 0)
            * float(item.get('height', 0) or 0)
            for item in packed
        ) / pallet_volume

    def _build_base_with_topup_candidate(
        self,
        items: List[Dict],
        target_mpm: float,
        pallet_dims: Dict,
    ) -> List[Dict]:
        """Build a dense base stack, then use remaining height for top-up boxes."""
        if not items or target_mpm <= 0:
            return []

        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        pallet_height = float(pallet_dims.get('height', 0) or 0)
        if pallet_length <= 0 or pallet_width <= 0 or pallet_height <= 0:
            return []

        groups: Dict[Tuple, List[Dict]] = {}
        for item in items:
            key = (
                item.get('type'),
                float(item.get('length', 0) or 0),
                float(item.get('width', 0) or 0),
                float(item.get('height', 0) or 0),
                float(item.get('min_pack_multiple', 0) or 0),
            )
            groups.setdefault(key, []).append(item)
        size_multiple_bonus = build_height_multiple_bonus_by_size(items)

        candidates: List[List[Dict]] = []
        for key, group in sorted(
            groups.items(),
            key=lambda entry: (
                -entry[0][4],
                -(entry[0][1] * entry[0][2] * entry[0][3]),
                str(entry[0][0]),
            ),
        ):
            _, raw_l, raw_w, raw_h, mpm = key
            if raw_l <= 0 or raw_w <= 0 or raw_h <= 0 or mpm <= 0:
                continue
            dims = {'length': raw_l + 2.0, 'width': raw_w + 2.0, 'height': raw_h}
            per_x = int(pallet_length // dims['length'])
            per_y = int(pallet_width // dims['width'])
            per_layer = per_x * per_y
            if per_layer <= 0:
                continue
            max_layers = min(int(pallet_height // dims['height']), len(group) // per_layer)
            for layer_count in range(max_layers, 0, -1):
                base_count = per_layer * layer_count
                base_mpm = base_count * mpm
                if (
                    base_mpm >= target_mpm
                    or base_mpm < target_mpm * 0.75
                    or mpm < 6.0
                ):
                    continue
                base_height = layer_count * dims['height']
                remaining_height = pallet_height - base_height
                if remaining_height <= 1e-9:
                    continue

                base = self._place_homogeneous_grid(
                    sort_same_size_heavier_first(group)[:base_count],
                    dims,
                    {'length': raw_l, 'width': raw_w, 'height': raw_h},
                    per_x,
                    per_y,
                    z_start=0.0,
                    placed=[],
                    pallet_dims=pallet_dims,
                )
                if not base:
                    continue
                used_ids = {item.get('id') for item in base}
                top_pool = [
                    item for item in items
                    if item.get('id') not in used_ids
                    and float(item.get('height', 0) or 0) <= remaining_height + 1e-9
                ]
                top_pool.sort(
                    key=lambda item: (
                        -float(item.get('min_pack_multiple', 0) or 0),
                        stacking_tiebreak_key(item, size_multiple_bonus),
                        str(item.get('id')),
                    )
                )
                topped = self._place_topup_rows(
                    top_pool, base, base_height, remaining_height, pallet_dims,
                    target_mpm - base_mpm,
                )
                if not topped:
                    continue
                candidate = base + topped
                total_mpm = sum(float(item.get('min_pack_multiple', 0) or 0) for item in candidate)
                if total_mpm >= target_mpm:
                    candidates.append(candidate)
                break

        return self._select_best_packed_candidate(candidates, items, target_mpm)

    def _place_homogeneous_grid(
        self,
        group: List[Dict],
        dims: Dict[str, float],
        raw_dims: Dict[str, float],
        per_x: int,
        per_y: int,
        z_start: float,
        placed: List[Dict],
        pallet_dims: Dict,
    ) -> List[Dict]:
        reachability = SuctionPlanner(
            pallet_dims=pallet_dims,
            suction_cup_length=self._cfg.suction_cup_length,
            suction_cup_width=self._cfg.suction_cup_width,
            suction_xy_clearance=self._cfg.suction_xy_clearance,
            suction_z_clearance=self._cfg.suction_z_clearance,
            allow_suction_rotation_90=self._cfg.suction_allow_rotation_90,
        )
        result: List[Dict] = []
        per_layer = per_x * per_y
        for idx, item in enumerate(group):
            layer_idx = idx // per_layer
            slot_idx = idx % per_layer
            point = {
                'x': float((slot_idx % per_x) * dims['length']),
                'y': float((slot_idx // per_x) * dims['width']),
                'z': float(z_start + layer_idx * dims['height']),
            }
            current = placed + result
            if point['z'] > 1e-9 and direct_support_ratio(point, dims, current) + 1e-9 < self._support_ratio:
                return []
            if self._same_size_enabled and not passes_same_size_heavier_below_constraint(
                item,
                point,
                dims,
                current,
            ):
                return []
            if self._reachability_enabled:
                suction_pose = reachability.find_reachable_suction_pose(
                    point, dims, current, raw_dims=raw_dims
                )
                if suction_pose is None:
                    return []
            else:
                suction_pose = None
            item_copy = deepcopy(item)
            item_copy['position'] = point
            item_copy['length'] = dims['length']
            item_copy['width'] = dims['width']
            item_copy['height'] = dims['height']
            item_copy['raw_length'] = raw_dims['length']
            item_copy['raw_width'] = raw_dims['width']
            item_copy['raw_height'] = raw_dims['height']
            if suction_pose is not None:
                apply_suction_pose_fields(item_copy, suction_pose)
            supported_area = calculate_direct_supported_area(point, dims, current)
            base_area = dims['length'] * dims['width']
            item_copy['supported_area'] = float(supported_area)
            item_copy['support_ratio'] = float(supported_area / base_area) if base_area > 0 else 0.0
            result.append(item_copy)
        return result

    def _place_topup_rows(
        self,
        pool: List[Dict],
        base: List[Dict],
        z_start: float,
        max_height: float,
        pallet_dims: Dict,
        required_mpm: float,
    ) -> List[Dict]:
        if required_mpm <= 0 or not pool:
            return []
        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        reachability = SuctionPlanner(
            pallet_dims=pallet_dims,
            suction_cup_length=self._cfg.suction_cup_length,
            suction_cup_width=self._cfg.suction_cup_width,
            suction_xy_clearance=self._cfg.suction_xy_clearance,
            suction_z_clearance=self._cfg.suction_z_clearance,
            allow_suction_rotation_90=self._cfg.suction_allow_rotation_90,
        )
        size_multiple_bonus = build_height_multiple_bonus_by_size(pool)
        ordered = sorted(
            pool,
            key=lambda item: (
                -float(item.get('min_pack_multiple', 0) or 0),
                stacking_tiebreak_key(item, size_multiple_bonus),
                -float(item.get('length', 0) or 0) * float(item.get('width', 0) or 0),
                str(item.get('id')),
            ),
        )
        placed_top: List[Dict] = []
        x = 0.0
        y = 0.0
        row_width = 0.0
        total_mpm = 0.0
        for item in ordered:
            raw_dims = {
                'length': float(item.get('length', 0) or 0),
                'width': float(item.get('width', 0) or 0),
                'height': float(item.get('height', 0) or 0),
            }
            dims = {
                'length': raw_dims['length'] + 2.0,
                'width': raw_dims['width'] + 2.0,
                'height': raw_dims['height'],
            }
            if (
                dims['length'] <= 0 or dims['width'] <= 0 or dims['height'] <= 0
                or dims['height'] > max_height + 1e-9
            ):
                continue
            if x + dims['length'] > pallet_length + 1e-9:
                x = 0.0
                y += row_width
                row_width = 0.0
            if y + dims['width'] > pallet_width + 1e-9:
                continue
            point = {'x': x, 'y': y, 'z': z_start}
            current = base + placed_top
            if direct_support_ratio(point, dims, current) + 1e-9 < self._support_ratio:
                x += dims['length']
                row_width = max(row_width, dims['width'])
                continue
            if not passes_same_size_heavier_below_constraint(
                item,
                point,
                dims,
                current,
            ):
                x += dims['length']
                row_width = max(row_width, dims['width'])
                continue
            if self._reachability_enabled:
                suction_pose = reachability.find_reachable_suction_pose(
                    point, dims, current, raw_dims=raw_dims
                )
                if suction_pose is None:
                    x += dims['length']
                    row_width = max(row_width, dims['width'])
                    continue
            else:
                suction_pose = None
            item_copy = deepcopy(item)
            item_copy['position'] = point
            item_copy['length'] = dims['length']
            item_copy['width'] = dims['width']
            item_copy['height'] = dims['height']
            item_copy['raw_length'] = raw_dims['length']
            item_copy['raw_width'] = raw_dims['width']
            item_copy['raw_height'] = raw_dims['height']
            if suction_pose is not None:
                apply_suction_pose_fields(item_copy, suction_pose)
            supported_area = calculate_direct_supported_area(point, dims, current)
            base_area = dims['length'] * dims['width']
            item_copy['supported_area'] = float(supported_area)
            item_copy['support_ratio'] = float(supported_area / base_area) if base_area > 0 else 0.0
            placed_top.append(item_copy)
            total_mpm += float(item.get('min_pack_multiple', 0) or 0)
            x += dims['length']
            row_width = max(row_width, dims['width'])
            if total_mpm + 1e-9 >= required_mpm:
                return placed_top
        return placed_top if total_mpm + 1e-9 >= required_mpm else []

    def _select_best_packed_candidate(
        self,
        candidates: List[List[Dict]],
        source_pool: List[Dict],
        target_mpm: Optional[float],
    ) -> List[Dict]:
        best_score = None
        best_packed: List[Dict] = []
        source_by_id = {item.get('id'): item for item in source_pool}
        best_potential = None
        for packed in candidates:
            packed_ids = {item.get('id') for item in packed}
            if not packed_ids or len(packed_ids) != len(packed):
                continue
            remaining = [
                item for item_id, item in source_by_id.items()
                if item_id not in packed_ids
            ]
            score, _, _, _ = PalletEvaluator.evaluate_pallet_solution(
                packed, remaining, target_mpm
            )
            potential = self._success_potential(
                packed, remaining, target_mpm
            )
            if (
                best_score is None
                or potential > best_potential
                or (potential == best_potential and score > best_score)
            ):
                best_potential = potential
                best_score = score
                best_packed = packed
        return best_packed

    def _top_up_current_pallet_to_fill(
        self,
        packed: List[Dict],
        remaining: List[Dict],
        pallet_dims: Dict,
        target_mpm: Optional[float],
        seed: int = 0,
        diag: Optional[Dict] = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        """继续向当前托盘补入仍可合法放置的箱子。

        主装箱阶段不能在还有可合法吸收的箱子时过早开新托盘。这里复用
        packer 的候选生成与放置校验，再用整盘约束和重心检查做最终门禁。
        """
        if not packed or not remaining:
            return packed, remaining

        probe = self._CustomPacker(
            pallet_dims,
            support_ratio_threshold=self._support_ratio,
            size_tolerance=2.0,
            max_candidate_points=180,
            max_points_per_layer=45,
            constraint_config=self._cfg,
        )
        if not hasattr(probe, "_generate_feasible_candidates"):
            return packed, remaining

        rng = random.Random(seed)
        placed = list(packed)
        left = list(remaining)
        placed_ids = {item.get('id') for item in placed}
        # placed 整盘合法时，每次试放只需做"新箱 vs 已放箱"的增量校验
        # （与整盘校验等价，O(N) 替代 O(N^2)）；placed 本就不合法的罕见
        # 情况退回原整盘校验路径（补箱理论上可修复既有间隙违例），
        # 保证行为与原实现严格一致。
        placed_geom_valid = self._is_valid_packed(placed, pallet_dims)

        while left:
            accepted = None
            current_success_potential = self._success_potential(
                placed, left, target_mpm
            )
            ordered = sorted(
                left,
                key=lambda item: (
                    -float(item.get('length', 0) or 0)
                    * float(item.get('width', 0) or 0)
                    * float(item.get('height', 0) or 0),
                    -float(item.get('min_pack_multiple', 0) or 0),
                    str(item.get('id')),
                ),
            )
            # 同型箱剪枝：尺寸/重量/指数/小箱标记全同的箱子，本轮可行性
            # 判定完全同构——一个试放失败即全型失败，直接跳过（每轮扫描
            # 从 O(剩余箱数) 降到 O(箱型数)，判定语义不变）。
            failed_types = set()
            for item in ordered:
                if item.get('id') in placed_ids:
                    continue
                type_key = (
                    round(float(item.get('length', 0) or 0), 1),
                    round(float(item.get('width', 0) or 0), 1),
                    round(float(item.get('height', 0) or 0), 1),
                    round(float(item.get('weight', 0) or 0), 3),
                    float(item.get('min_pack_multiple', 0) or 0),
                    bool(item.get('is_small_box')),
                )
                if type_key in failed_types:
                    continue
                candidates = probe._generate_feasible_candidates(
                    item, placed, rng
                )
                for candidate in sorted(
                    candidates, key=lambda entry: entry.get('score', ())
                ):
                    if diag is not None:
                        diag["main_topup_attempts"] = (
                            diag.get("main_topup_attempts", 0) + 1
                        )
                    candidate_box = candidate.get('box')
                    if not candidate_box:
                        continue
                    trial = placed + [candidate_box]
                    trial_left = [
                        candidate_left for candidate_left in left
                        if candidate_left.get('id') != item.get('id')
                    ]
                    if (
                        self._success_potential(
                            trial, trial_left, target_mpm
                        )
                        < current_success_potential
                    ):
                        if diag is not None:
                            diag["main_topup_rejected_future_success"] = (
                                diag.get(
                                    "main_topup_rejected_future_success", 0
                                ) + 1
                            )
                        continue
                    trial_geom_ok = (
                        incremental_pallet_ok(
                            candidate_box, placed, pallet_dims,
                            constraint_config=self._cfg,
                        )
                        if placed_geom_valid
                        else self._is_valid_packed(trial, pallet_dims)
                    )
                    if not trial_geom_ok:
                        if diag is not None:
                            diag["main_topup_rejected_constraints"] = (
                                diag.get(
                                    "main_topup_rejected_constraints", 0
                                ) + 1
                            )
                        continue
                    if not self._validate_com(
                        {'packed_items': trial, 'mpm_target': target_mpm},
                        pallet_dims,
                    ).get('is_stable', False):
                        if diag is not None:
                            diag["main_topup_rejected_constraints"] = (
                                diag.get(
                                    "main_topup_rejected_constraints", 0
                                ) + 1
                            )
                        continue
                    accepted = (item, candidate_box)
                    break
                if accepted is not None:
                    break
                failed_types.add(type_key)

            if accepted is None:
                break

            source_item, placed_item = accepted
            placed.append(placed_item)
            if not placed_geom_valid:
                # 经整盘校验路径接受的 trial 即新的 placed，已整盘合法，
                # 后续试放可切换到增量校验。
                placed_geom_valid = True
            if diag is not None:
                diag["main_topup_accepted"] = (
                    diag.get("main_topup_accepted", 0) + 1
                )
            placed_ids.add(source_item.get('id'))
            left = [
                item for item in left
                if item.get('id') != source_item.get('id')
            ]

        return placed, left

    def _success_potential(
        self,
        packed: List[Dict],
        remaining: List[Dict],
        target_mpm: Optional[float],
    ) -> int:
        if target_mpm is None or target_mpm <= 0:
            return 0
        packed_mpm = sum(
            float(item.get('min_pack_multiple', 0) or 0)
            for item in packed
        )
        remaining_mpm = sum(
            float(item.get('min_pack_multiple', 0) or 0)
            for item in remaining
        )
        current_success = 1 if packed_mpm + 1e-9 >= target_mpm else 0
        return current_success + int(remaining_mpm // target_mpm)

    def _is_valid_packed(
        self, packed: List[Dict], pallet_dims: Dict
    ) -> bool:
        return validate_pallet_constraints(
            {"packed_items": packed}, pallet_dims,
            constraint_config=self._cfg,
        )["is_valid"]

    def _retry_for_index(
        self,
        best: Dict,
        unfitted: List[Dict],
        target_mpm: float,
        pallet_dims: Dict,
        pallet_counter: int,
        fill_aware: bool = True,
    ) -> Dict:
        """指数优先重试：根据缺口和潜力自适应分配预算。"""
        base_total = best["total_mpm"]
        remaining = best["remaining_unfitted"]
        remaining_count = len(remaining)
        base_count = max(1, len(unfitted))
        gap = max(0.0, target_mpm - base_total)
        remaining_mpm = sum(
            float(b.get('min_pack_multiple', 0) or 0) for b in remaining
        )
        potential = remaining_mpm / max(gap, 1.0)

        if gap <= 8 and potential >= 1.6:
            max_retry, restart_n, beam_w, cand_n = 4, 6, 2, 9
        elif gap <= 24 and potential >= 1.1:
            max_retry, restart_n, beam_w, cand_n = 2, 4, 1, 7
        else:
            max_retry, restart_n, beam_w, cand_n = 1, 3, 1, 6

        if remaining_count <= 20 and gap <= 12 and potential >= 1.4:
            max_retry = min(5, max_retry + 1)

        if base_count > 1000:
            max_retry = 1 if gap <= 8 else 0
            restart_n, beam_w, cand_n = 2, 1, 5
        elif base_count > 500:
            max_retry = min(max_retry, 1)

        no_gain = 0
        best_seen = best["total_mpm"]
        for retry_seed in range(max_retry):
            packer = self._CustomPacker(
                pallet_dims,
                support_ratio_threshold=self._support_ratio,
                size_tolerance=2.0,
                max_candidate_points=150,
                max_points_per_layer=30,
                constraint_config=self._cfg,
            )
            expand = 1.1 + retry_seed * 0.3
            if fill_aware:
                pool = IndexBuilder.build_pallet_candidate_pool(
                    unfitted,
                    target_mpm=target_mpm,
                    seed=(pallet_counter * 97 + retry_seed),
                    pallet_dims=pallet_dims,
                    max_items=int(96 * expand),
                )
            else:
                pool = IndexBuilder.build_index_bucket_candidate_pool(
                    unfitted,
                    target_mpm=target_mpm,
                    seed=(pallet_counter * 97 + retry_seed),
                    expand_factor=expand,
                    pallet_dims=pallet_dims,
                )
            packed, _ = packer.pack(
                pool,
                num_restarts=restart_n,
                beam_width=max(beam_w, 2),
                candidate_limit=cand_n,
                random_seed=retry_seed + 100,
                target_mpm=target_mpm,
                stop_when_target_met=False if fill_aware else True,
            )
            if packed and not self._is_valid_packed(packed, pallet_dims):
                continue
            packed_ids = {b['id'] for b in packed}
            r_unfit = [b for b in unfitted if b['id'] not in packed_ids]
            r_score, r_total, _, _ = PalletEvaluator.evaluate_pallet_solution(
                packed, r_unfit, target_mpm
            )
            improvement = r_total - best_seen
            if r_score > best["score"]:
                best = {
                    "packed_items": packed,
                    "remaining_unfitted": r_unfit,
                    "score": r_score,
                    "total_mpm": r_total,
                }
            if improvement >= 1:
                best_seen = max(best_seen, r_total)
                no_gain = 0
            else:
                no_gain += 1
            if no_gain >= 2:
                break
        return best
