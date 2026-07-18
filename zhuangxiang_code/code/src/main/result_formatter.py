"""
结果格式化器

汇总分组统计、构建 KPI、输出 JSON 报告。
"""

from typing import Callable, Dict, List, Optional

from src.rescue import PalletEvaluator


class ResultFormatter:
    """结果汇总与格式化。"""

    @staticmethod
    def _tier_counts(plans: List[Dict]) -> Dict[str, int]:
        near = mid = deep = 0
        for p in plans:
            if p.get('mpm_status') != 'FAILED':
                continue
            gap = float(p.get('mpm_gap') or 0)
            if 0 < gap <= 8:
                near += 1
            elif 8 < gap <= 24:
                mid += 1
            elif gap > 24:
                deep += 1
        return {"near": near, "mid": mid, "deep": deep}

    @staticmethod
    def build_type_stats(
        type_plan: List[Dict],
        pallet_type: str,
        sales_order_no: str,
        index_diag: Dict,
        rescued_cnt: int,
        runtime: Dict[str, float],
        repack_result: Dict,
        hole_fill_result: Optional[Dict] = None,
        topup_result: Optional[Dict] = None,
        recipe_rebuild_result: Optional[Dict] = None,
        low_load_result: Optional[Dict] = None,
        tail_absorb_result: Optional[Dict] = None,
        low_fill_result: Optional[Dict] = None,
        failed_pool_result: Optional[Dict] = None,
    ) -> Dict:
        """构建单分组的统计信息。"""
        stats = PalletEvaluator.recompute_type_stats(type_plan)
        stats["pallet_type"] = pallet_type
        stats["sales_order_no"] = sales_order_no
        stats["index_diagnostics"] = index_diag
        stats["rescued_from_failed"] = rescued_cnt
        fills = [
            float(plan.get("fill_rate") or 0.0)
            for plan in type_plan
            if plan.get("packed_items")
        ]
        low_fill_failed = [
            plan for plan in type_plan
            if plan.get("mpm_status") == "FAILED"
            and float(plan.get("fill_rate") or 0.0) < 0.3
        ]
        stats["diagnosis"] = {
            "index_unreachable_order": bool(
                index_diag.get("index_target_unreachable", False)
            ),
            "geometric_unreachable_order": bool(
                index_diag.get("geometric_target_unreachable", False)
            ),
            "algorithm_underfilled_order": bool(low_fill_failed),
            "low_fill_failed_pallets": len(low_fill_failed),
            "avg_fill_rate": round(
                sum(fills) / max(1, len(fills)), 6
            ),
        }

        tiers = ResultFormatter._tier_counts(type_plan)
        failed_cnt = stats["failed_pallets"]
        rescue_rate = (rescued_cnt / failed_cnt) if failed_cnt > 0 else 0.0
        pair_tried = repack_result.get("pair_tried", 0)
        pair_improved = repack_result.get("pair_improved", 0)
        pair_eff = pair_improved / max(1, pair_tried)

        hf = hole_fill_result or {}
        tu = topup_result or {}
        rb = recipe_rebuild_result or {}
        ll = low_load_result or {}
        ta = tail_absorb_result or {}
        lf = low_fill_result or {}
        fp = failed_pool_result or {}

        stats["kpi"] = {
            "failed_near_count": tiers["near"],
            "failed_mid_count": tiers["mid"],
            "failed_deep_count": tiers["deep"],
            "rescue_rate_from_failed": round(rescue_rate, 4),
            "pair_efficiency": round(pair_eff, 4),
            "pair_tried": pair_tried,
            "pair_improved": pair_improved,
            "pair_budget": repack_result.get("pair_budget", 0),
            "consolidate_tried": repack_result.get("consolidate_tried", 0),
            "consolidate_accepted": repack_result.get("consolidate_accepted", 0),
            "consolidate_old_pallets": repack_result.get("consolidate_old_pallets", 0),
            "consolidate_new_pallets": repack_result.get("consolidate_new_pallets", 0),
            "consolidate_new_success": repack_result.get("consolidate_new_success", 0),
            "consolidate_old_avg_fill": round(
                repack_result.get("consolidate_old_avg_fill", 0.0), 4
            ),
            "consolidate_new_avg_fill": round(
                repack_result.get("consolidate_new_avg_fill", 0.0), 4
            ),
            "consolidate_reason": repack_result.get("consolidate_reason", ""),
            "extract_commit_success": repack_result.get(
                "extract_commit_success", 0
            ),
            "redistribute_tried": repack_result.get("redistribute_tried", 0),
            "redistribute_accepted": repack_result.get(
                "redistribute_accepted", 0
            ),
            "redistribute_dissolved": repack_result.get(
                "redistribute_dissolved", 0
            ),
            "redistribute_reason": repack_result.get("redistribute_reason", ""),
            "fill_compact_merges": repack_result.get("fill_compact_merges", 0),
            "fill_compact_reason": repack_result.get(
                "fill_compact_reason", ""
            ),
            "swap_tried": repack_result.get("swap_tried", 0),
            "swap_accepted": repack_result.get("swap_accepted", 0),
            "swap_surplus_total": round(
                repack_result.get("swap_surplus_total", 0.0), 3
            ),
            "swap_reason": repack_result.get("swap_reason", ""),
            "targeted_tried": repack_result.get("targeted_tried", 0),
            "targeted_success": repack_result.get("targeted_success", 0),
            "targeted_unreachable": repack_result.get("targeted_unreachable", 0),
            "targeted_geofail": repack_result.get("targeted_geofail", 0),
            "targeted_nonimprove": repack_result.get("targeted_nonimprove", 0),
            "hole_fill_tried": hf.get("hole_fill_tried", 0),
            "hole_fill_success": hf.get("hole_fill_success", 0),
            "hole_fill_pack_fail": hf.get("hole_fill_pack_fail", 0),
            "topup_tried": tu.get("topup_tried", 0),
            "topup_success": tu.get("topup_success", 0),
            "topup_pack_fail": tu.get("topup_pack_fail", 0),
            "topup_rejected_missing_receiver": tu.get(
                "topup_rejected_missing_receiver", 0
            ),
            "recipe_rebuild_tried": rb.get("recipe_rebuild_tried", 0),
            "recipe_rebuild_success": rb.get("recipe_rebuild_success", 0),
            "recipe_rebuild_rescued": rb.get("rescued", 0),
            "recipe_rebuild_old_success": rb.get("recipe_rebuild_old_success", 0),
            "recipe_rebuild_new_success": rb.get("recipe_rebuild_new_success", 0),
            "failed_pool_rescued": fp.get("rescued", 0),
            "failed_pool_attempts": fp.get("rebuild_attempts", 0),
            "failed_pool_theoretical_success": fp.get("rebuild_theoretical_success", 0),
            "failed_pool_success_pallets": fp.get("rebuild_success_pallets", 0),
            "failed_pool_leftover_pallets": fp.get("rebuild_leftover_pallets", 0),
            "failed_pool_geometry_failures": fp.get("geometry_failures", 0),
            "kto1_tried": repack_result.get("kto1_tried", 0),
            "kto1_success": repack_result.get("kto1_success", 0),
            "kto1_pack_fail": repack_result.get("kto1_pack_fail", 0),
            "kto1_no_improve": repack_result.get("kto1_no_improve", 0),
            "fallback_triggered": bool(
                repack_result.get("fallback_triggered", False)
            ),
            "low_load_tried": ll.get("low_load_tried", 0),
            "low_load_accepted": ll.get("low_load_accepted", 0),
            "low_load_selected_pallets": ll.get("low_load_selected_pallets", 0),
            "low_load_old_pallets": ll.get("low_load_old_pallets", 0),
            "low_load_new_pallets": ll.get("low_load_new_pallets", 0),
            "low_load_old_success": ll.get("low_load_old_success", 0),
            "low_load_new_success": ll.get("low_load_new_success", 0),
            "low_pair_tried": ll.get("low_pair_tried", 0),
            "low_pair_accepted": ll.get("low_pair_accepted", 0),
            "low_pair_emptied": ll.get("low_pair_emptied", 0),
            "tail_absorb_tried": ta.get("tail_absorb_tried", 0),
            "tail_absorb_success": ta.get("tail_absorb_success", 0),
            "tail_absorb_donor_emptied": ta.get(
                "tail_absorb_donor_emptied", 0
            ),
            "tail_absorb_rejected": ta.get("tail_absorb_rejected", 0),
            "tail_absorb_pack_fail": ta.get("tail_absorb_pack_fail", 0),
            "tail_absorb_old_success": ta.get(
                "tail_absorb_old_success", 0
            ),
            "tail_absorb_new_success": ta.get(
                "tail_absorb_new_success", 0
            ),
            "tail_absorb_old_low_count": ta.get(
                "tail_absorb_old_low_count", 0
            ),
            "tail_absorb_new_low_count": ta.get(
                "tail_absorb_new_low_count", 0
            ),
            "low_fill_tried": lf.get("low_fill_tried", 0),
            "low_fill_accepted": lf.get("low_fill_accepted", 0),
            "low_fill_old_pallets": lf.get("low_fill_old_pallets", 0),
            "low_fill_new_pallets": lf.get("low_fill_new_pallets", 0),
            "low_fill_old_success": lf.get("low_fill_old_success", 0),
            "low_fill_new_success": lf.get("low_fill_new_success", 0),
            "low_fill_old_avg_fill": lf.get("low_fill_old_avg_fill", 0.0),
            "low_fill_new_avg_fill": lf.get("low_fill_new_avg_fill", 0.0),
        }
        stats["runtime_breakdown_seconds"] = {
            "packing": round(runtime.get("packing", 0.0), 2),
            "topup": round(runtime.get("topup", 0.0), 2),
            "retry": round(runtime.get("retry", 0.0), 2),
            "repack": round(runtime.get("repack", 0.0), 2),
            "failed_pool": round(runtime.get("failed_pool_seconds", 0.0), 2),
            "hole_fill": round(runtime.get("hole_fill_seconds", 0.0), 2),
            "topup_rescue": round(runtime.get("topup_rescue_seconds", 0.0), 2),
            "recipe_rebuild": round(runtime.get("recipe_rebuild_seconds", 0.0), 2),
            "pair_repack": round(runtime.get("pair_repack_seconds", 0.0), 2),
            "low_fill": round(runtime.get("low_fill_seconds", 0.0), 2),
            "tail_absorb": round(runtime.get("tail_absorb_seconds", 0.0), 2),
            "low_load": round(runtime.get("low_load_seconds", 0.0), 2),
            "low_pair": round(runtime.get("low_pair_seconds", 0.0), 2),
            "total": round(runtime.get("total", 0.0), 2),
        }
        return stats

    @staticmethod
    def build_overall_summary(
        final_plan: List[Dict],
        by_type_stats: Dict[str, Dict],
        runtime_stats: Dict[str, float],
        total_runtime: float,
    ) -> Dict:
        """根据所有分组结果构建总体统计。"""
        overall = {
            "total_pallets": 0,
            "success_pallets": 0,
            "failed_pallets": 0,
            "unknown_pallets": 0,
            "avg_mpm_gap": 0.0,
            "max_mpm_gap": 0.0,
            "rescued_from_failed": 0,
            "index_unreachable_orders": 0,
            "geometric_unreachable_orders": 0,
            "algorithm_underfilled_orders": 0,
            "low_fill_failed_pallets": 0,
        }
        for ts in by_type_stats.values():
            overall["total_pallets"] += ts["total_pallets"]
            overall["success_pallets"] += ts["success_pallets"]
            overall["failed_pallets"] += ts["failed_pallets"]
            overall["unknown_pallets"] += ts["unknown_pallets"]
            overall["rescued_from_failed"] += ts.get("rescued_from_failed", 0)
            diagnosis = ts.get("diagnosis", {})
            overall["index_unreachable_orders"] += int(
                bool(diagnosis.get("index_unreachable_order"))
            )
            overall["geometric_unreachable_orders"] += int(
                bool(diagnosis.get("geometric_unreachable_order"))
            )
            overall["algorithm_underfilled_orders"] += int(
                bool(diagnosis.get("algorithm_underfilled_order"))
            )
            overall["low_fill_failed_pallets"] += int(
                diagnosis.get("low_fill_failed_pallets", 0) or 0
            )
            overall["max_mpm_gap"] = max(
                overall["max_mpm_gap"], ts["max_mpm_gap"]
            )
            overall["avg_mpm_gap"] += ts["avg_mpm_gap"] * ts["failed_pallets"]

        if overall["failed_pallets"] > 0:
            overall["avg_mpm_gap"] = round(
                overall["avg_mpm_gap"] / overall["failed_pallets"], 2
            )
        else:
            overall["avg_mpm_gap"] = 0.0

        tiers = ResultFormatter._tier_counts(final_plan)

        def _sum(key):
            return sum(
                v.get("kpi", {}).get(key, 0) for v in by_type_stats.values()
            )

        pair_tried = _sum("pair_tried")
        pair_improved = _sum("pair_improved")
        return {
            **overall,
            "kpi": {
                "failed_near_count": tiers["near"],
                "failed_mid_count": tiers["mid"],
                "failed_deep_count": tiers["deep"],
                "pair_tried": pair_tried,
                "pair_improved": pair_improved,
                "pair_efficiency": round(
                    pair_improved / max(1, pair_tried), 4
                ),
                "hole_fill_tried": _sum("hole_fill_tried"),
                "hole_fill_success": _sum("hole_fill_success"),
                "topup_tried": _sum("topup_tried"),
                "topup_success": _sum("topup_success"),
                "recipe_rebuild_tried": _sum("recipe_rebuild_tried"),
                "recipe_rebuild_success": _sum("recipe_rebuild_success"),
                "recipe_rebuild_rescued": _sum("recipe_rebuild_rescued"),
                "failed_pool_rescued": _sum("failed_pool_rescued"),
                "failed_pool_attempts": _sum("failed_pool_attempts"),
                "failed_pool_theoretical_success": _sum(
                    "failed_pool_theoretical_success"
                ),
                "failed_pool_success_pallets": _sum("failed_pool_success_pallets"),
                "kto1_tried": _sum("kto1_tried"),
                "kto1_success": _sum("kto1_success"),
                "low_load_tried": _sum("low_load_tried"),
                "low_load_accepted": _sum("low_load_accepted"),
                "low_load_selected_pallets": _sum("low_load_selected_pallets"),
                "low_load_old_pallets": _sum("low_load_old_pallets"),
                "low_load_new_pallets": _sum("low_load_new_pallets"),
                "low_load_old_success": _sum("low_load_old_success"),
                "low_load_new_success": _sum("low_load_new_success"),
                "low_pair_tried": _sum("low_pair_tried"),
                "low_pair_accepted": _sum("low_pair_accepted"),
                "low_pair_emptied": _sum("low_pair_emptied"),
                "tail_absorb_tried": _sum("tail_absorb_tried"),
                "tail_absorb_success": _sum("tail_absorb_success"),
                "tail_absorb_donor_emptied": _sum(
                    "tail_absorb_donor_emptied"
                ),
                "tail_absorb_rejected": _sum("tail_absorb_rejected"),
                "tail_absorb_pack_fail": _sum("tail_absorb_pack_fail"),
                "tail_absorb_old_success": _sum("tail_absorb_old_success"),
                "tail_absorb_new_success": _sum("tail_absorb_new_success"),
                "tail_absorb_old_low_count": _sum(
                    "tail_absorb_old_low_count"
                ),
                "tail_absorb_new_low_count": _sum(
                    "tail_absorb_new_low_count"
                ),
                "low_fill_tried": _sum("low_fill_tried"),
                "low_fill_accepted": _sum("low_fill_accepted"),
                "low_fill_old_pallets": _sum("low_fill_old_pallets"),
                "low_fill_new_pallets": _sum("low_fill_new_pallets"),
                "low_fill_old_success": _sum("low_fill_old_success"),
                "low_fill_new_success": _sum("low_fill_new_success"),
            },
            "runtime_breakdown_seconds": {
                "packing": round(runtime_stats.get("group_pack_seconds", 0.0), 2),
                "topup": round(runtime_stats.get("group_topup_seconds", 0.0), 2),
                "retry": round(runtime_stats.get("group_retry_seconds", 0.0), 2),
                "repack": round(runtime_stats.get("group_repack_seconds", 0.0), 2),
                "group_total": round(
                    runtime_stats.get("group_total_seconds", 0.0), 2
                ),
                "end_to_end": round(total_runtime, 2),
            },
        }

    @staticmethod
    def build_full_report(
        final_plan: List[Dict],
        summary_stats: Dict,
        total_runtime: float,
        raw_boxes: List[Dict],
        make_json_plan_fn: Callable,
    ) -> Dict:
        """组装最终 JSON 报告。"""
        pallets = make_json_plan_fn(final_plan, raw_boxes)
        ResultFormatter.validate_output_quality(raw_boxes, pallets)

        # 装箱几何完成后再生成机器人执行顺序：只增补顺序/依赖/吸盘字段，
        # 不改变托盘、箱子位置、尺寸、朝向或 packed_items 原始列表顺序。
        from src.postprocess.robot_sequence import apply_robot_sequences
        robot_sequence_summary = apply_robot_sequences(pallets)

        return {
            "packing_plan_id": None,
            "total_runtime_seconds": round(total_runtime, 2),
            "summary": summary_stats,
            "robot_sequence_summary": robot_sequence_summary,
            "pallets": pallets,
        }

    @staticmethod
    def validate_output_quality(raw_boxes: List[Dict], pallets: List[Dict]) -> None:
        """业务输出质量门禁：不允许漏箱、重箱、空托盘、零尺寸箱或 case_group 混装。

        case_group 纯度用输入箱 id→case_group 映射校验（权威口径，不依赖输出
        字段透传）：同一托盘上所有箱子的归一化 case_group 必须一致。
        """
        from src.utils.case_group import normalize_case_group

        input_ids = [str(box.get('id')) for box in raw_boxes]
        input_id_set = set(input_ids)
        cg_by_id = {
            str(box.get('id')): normalize_case_group(box.get('case_group'))
            for box in raw_boxes
        }
        output_ids = []
        empty_pallet_ids = []
        zero_dimension_boxes = []
        case_group_mixed = []

        for pallet in pallets:
            items = pallet.get('packed_items', [])
            if not items:
                empty_pallet_ids.append(pallet.get('pallet_id'))
                continue
            pallet_groups = {
                cg_by_id[str(item.get('id'))]
                for item in items
                if str(item.get('id')) in cg_by_id
            }
            if len(pallet_groups) > 1:
                case_group_mixed.append(pallet.get('pallet_id'))
            for item in items:
                item_id = str(item.get('id'))
                output_ids.append(item_id)
                length = float(item.get('length', 0) or 0)
                width = float(item.get('width', 0) or 0)
                height = float(item.get('height', 0) or 0)
                volume = float(
                    item.get('volume', length * width * height) or 0
                )
                if (
                    length <= 0
                    or width <= 0
                    or height <= 0
                    or volume <= 0
                ):
                    zero_dimension_boxes.append(item_id)

        output_id_set = set(output_ids)
        missing_ids = sorted(input_id_set - output_id_set)
        extra_ids = sorted(output_id_set - input_id_set)
        duplicate_ids = sorted(
            {
                item_id
                for item_id in output_ids
                if output_ids.count(item_id) > 1
            }
        )

        violations = []
        if empty_pallet_ids:
            violations.append(f"empty_pallets={empty_pallet_ids[:5]}")
        if missing_ids:
            violations.append(f"missing_boxes={missing_ids[:5]}")
        if extra_ids:
            violations.append(f"extra_boxes={extra_ids[:5]}")
        if duplicate_ids:
            violations.append(f"duplicate_boxes={duplicate_ids[:5]}")
        if zero_dimension_boxes:
            violations.append(f"zero_dimension_boxes={zero_dimension_boxes[:5]}")
        if case_group_mixed:
            violations.append(f"case_group_mixed_pallets={case_group_mixed[:5]}")

        if violations:
            raise ValueError("输出质量门禁失败：" + "; ".join(violations))
