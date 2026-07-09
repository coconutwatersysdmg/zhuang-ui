"""
失败托盘合池重建器

Phase 6+ 新策略：放弃"保留 receiver 现状再加箱"的旧救援假设，
把分组内所有 FAILED 托盘的箱子合并为自由箱池，按 target_mpm 重新背包+装箱，
生成新的 SUCCESS 托盘并替换最差的失败盘。
"""

from typing import Callable, Dict, List, Optional, Tuple

from src.geometry.constraint_validator import validate_plan_constraints
from src.utils.helpers import repack_ready_item

from .index_builder import IndexBuilder
from .pallet_evaluator import PalletEvaluator


class FailedPoolRebuilder:
    """失败托盘合池重建器。

    把分组内所有 FAILED 托盘的箱子合并为一个自由箱池，反复用
    IndexBuilder.build_index_bucket 选出 mpm ≈ target 的子集，
    再用几何装箱原语验证。能装出新的 SUCCESS 托盘就替换最差的 FAILED 托盘。
    剩余未进入新盘的箱子打包为 leftover 托盘补回。
    """

    def __init__(
        self,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        validate_center_of_mass: Callable,
        max_geometry_fail_streak: int = 3,
        max_total_attempts: int = 24,
        constraint_config=None,
    ):
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._CustomPacker = custom_packer_cls
        self._build_direct_layer = build_direct_layer_solution
        self._validate_com = validate_center_of_mass
        self._max_geo_fail_streak = max_geometry_fail_streak
        self._max_attempts = max_total_attempts

    def rebuild(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
    ) -> Dict:
        """合池重建。直接修改 type_plans，返回诊断字典。

        Returns:
            {
                "rescued": int,                # 救回的失败盘数
                "rebuild_attempts": int,       # 总装箱尝试次数
                "rebuild_success_pallets": int,# 装出的新 SUCCESS 盘数
                "rebuild_leftover_pallets":int,# 残余箱子产生的 leftover 盘数
                "geometry_failures": int,
                "skipped": bool,               # 是否因前置条件不满足而跳过
                "reason": str,                 # 跳过原因
                "box_conservation_ok": bool,
            }
        """
        diag = {
            "rescued": 0,
            "rebuild_attempts": 0,
            "rebuild_success_pallets": 0,
            "rebuild_leftover_pallets": 0,
            "geometry_failures": 0,
            "skipped": True,
            "reason": "",
            "box_conservation_ok": True,
        }
        if target_mpm is None:
            diag["reason"] = "no_target"
            return diag
        if not type_plans:
            diag["reason"] = "empty_plans"
            return diag

        # 确保每个托盘的状态字段最新
        for p in type_plans:
            PalletEvaluator.calc_pallet_status(p)

        failed = [p for p in type_plans if p.get('mpm_status') == 'FAILED']
        if len(failed) < 2:
            diag["reason"] = "less_than_2_failed"
            return diag

        # 收集自由箱池：把 FAILED 盘的箱子还原为可重装状态
        pool: List[Dict] = []
        for plan in failed:
            for item in plan.get('packed_items', []):
                pool.append(repack_ready_item(item))

        original_ids = {b['id'] for b in pool}
        total_pool_mpm = sum(
            float(b.get('min_pack_multiple', 0) or 0) for b in pool
        )

        if total_pool_mpm < target_mpm:
            diag["reason"] = "pool_mpm_below_target"
            return diag

        diag["skipped"] = False
        theoretical_success = int(total_pool_mpm // target_mpm)
        max_attempts = min(
            max(self._max_attempts, theoretical_success * 3),
            160,
        )
        diag["rebuild_theoretical_success"] = theoretical_success

        # 反复挑桶 + 几何验证
        new_success_pallets: List[List[Dict]] = []
        geo_fail_streak = 0
        attempts = 0
        seed = 9001
        while pool and attempts < max_attempts:
            remaining_mpm = sum(
                float(b.get('min_pack_multiple', 0) or 0) for b in pool
            )
            if remaining_mpm < target_mpm:
                diag["reason"] = "pool_drained"
                break

            attempts += 1
            diag["rebuild_attempts"] = attempts

            packed = self._build_direct_layer(
                pool,
                target_mpm=target_mpm,
                pallet_dims=pallet_dims,
                seed=seed,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                candidate_count=16,
                constraint_config=self._cfg,
            )
            seed += 17
            if packed:
                packed_mpm = sum(
                    float(b.get('min_pack_multiple', 0) or 0)
                    for b in packed
                )
                if packed_mpm >= target_mpm:
                    used_ids = {b['id'] for b in packed}
                    pool = [b for b in pool if b['id'] not in used_ids]
                    new_success_pallets.append(packed)
                    geo_fail_streak = 0
                    if len(new_success_pallets) >= theoretical_success:
                        diag["reason"] = "theoretical_success_reached"
                        break
                    continue

            # 用候选池而非单纯背包桶：给装箱器更多冗余以挑选可行子集。
            # expand_factor 随重试增长，扩大候选规模。
            expand_factor = 1.5 + (attempts - 1) * 0.4
            candidate_pool = IndexBuilder.build_index_bucket_candidate_pool(
                pool,
                target_mpm=target_mpm,
                seed=seed,
                expand_factor=expand_factor,
                pallet_dims=pallet_dims,
            )
            seed += 17

            if not candidate_pool:
                geo_fail_streak += 1
                if geo_fail_streak >= self._max_geo_fail_streak:
                    diag["reason"] = "bucket_search_exhausted"
                    break
                continue

            packed = self._geometry_pack(
                candidate_pool, pallet_dims, target_mpm, seed
            )
            if not packed:
                diag["geometry_failures"] += 1
                geo_fail_streak += 1
                if geo_fail_streak >= self._max_geo_fail_streak:
                    diag["reason"] = "geometry_fail_streak"
                    break
                continue

            packed_mpm = sum(
                float(b.get('min_pack_multiple', 0) or 0) for b in packed
            )
            if packed_mpm < target_mpm:
                # 没达到目标，丢弃这次尝试
                geo_fail_streak += 1
                diag["geometry_failures"] += 1
                if geo_fail_streak >= self._max_geo_fail_streak:
                    diag["reason"] = "packed_mpm_below_target"
                    break
                continue

            # 接受这次重建：从池中移除已用箱子
            used_ids = {b['id'] for b in packed}
            pool = [b for b in pool if b['id'] not in used_ids]
            new_success_pallets.append(packed)
            geo_fail_streak = 0
            if len(new_success_pallets) >= theoretical_success:
                diag["reason"] = "theoretical_success_reached"
                break

        if not new_success_pallets:
            if not diag["reason"]:
                diag["reason"] = "no_success_built"
            return diag

        used_success_ids = {
            b['id']
            for packed in new_success_pallets
            for b in packed
        }
        leftover_pallets = self._preserve_leftover_failed_layouts(
            failed,
            used_success_ids,
            pallet_dims,
            target_mpm,
        )
        if leftover_pallets is None:
            # 保留原布局不合法时再回退到全池重打包。
            leftover_pallets = self._pack_leftovers(
                pool, pallet_dims, target_mpm
            )

        # 守恒检查
        rebuilt_ids = set()
        for p in new_success_pallets:
            rebuilt_ids.update(b['id'] for b in p)
        for p in leftover_pallets:
            rebuilt_ids.update(b['id'] for b in p)
        leftover_unplaced = [b for b in pool if b['id'] not in rebuilt_ids]
        if leftover_unplaced:
            diag["box_conservation_ok"] = False
            diag["reason"] = "leftover_unplaced_not_packable"
            return diag

        if rebuilt_ids != original_ids:
            diag["box_conservation_ok"] = False
            diag["reason"] = "box_conservation_failed"
            return diag  # 不接受改动

        # 严格优于性：只有新 SUCCESS 数 > 0 才接受
        # 此时一定 > 0（new_success_pallets 非空且全部 packed_mpm >= target）
        replaced = self._apply_replacement(
            type_plans,
            failed,
            new_success_pallets,
            leftover_pallets,
            pallet_dims,
            target_mpm,
        )
        if replaced < 0:
            diag["reason"] = "constraint_gate_failed"
            return diag
        diag["rescued"] = replaced
        diag["rebuild_success_pallets"] = len(new_success_pallets)
        diag["rebuild_leftover_pallets"] = len(leftover_pallets)
        if not diag["reason"]:
            diag["reason"] = "ok"
        return diag

    def _preserve_leftover_failed_layouts(
        self,
        failed_plans: List[Dict],
        used_ids: set,
        pallet_dims: Dict,
        target_mpm: float,
    ) -> Optional[List[List[Dict]]]:
        preserved: List[List[Dict]] = []
        for plan in failed_plans:
            items = [
                item for item in plan.get('packed_items', [])
                if item.get('id') not in used_ids
            ]
            if not items:
                continue
            solution = {
                "packed_items": items,
                "mpm_target": target_mpm,
            }
            if not validate_plan_constraints(
                [solution], pallet_dims, constraint_config=self._cfg
            )["is_valid"]:
                return None
            preserved.append(items)
        return preserved

    def _geometry_pack(
        self,
        bucket: List[Dict],
        pallet_dims: Dict,
        target_mpm: float,
        seed: int,
    ) -> List[Dict]:
        """几何验证链：直接整层 -> CustomPacker（高搜索预算）。"""
        packed = self._build_direct_layer(
            bucket,
            target_mpm=target_mpm,
            pallet_dims=pallet_dims,
            seed=seed,
            xy_tolerance=2.0,
            z_tolerance=0.0,
            candidate_count=14,
            constraint_config=self._cfg,
        )
        if packed:
            packed_mpm = sum(
                float(b.get('min_pack_multiple', 0) or 0) for b in packed
            )
            if packed_mpm >= target_mpm:
                return packed

        # CustomPacker：救援场景给它更大的搜索预算，且开启 allow_skip
        # 以便丢弃几何不兼容的箱子但仍尽量达标。
        packer = self._CustomPacker(
            pallet_dims,
            support_ratio_threshold=self._cfg.support_ratio_threshold,
            size_tolerance=2.0,
            max_candidate_points=180,
            max_points_per_layer=36,
            constraint_config=self._cfg,
        )
        packed, _ = packer.pack(
            bucket,
            num_restarts=8,
            beam_width=3,
            candidate_limit=10,
            random_seed=seed,
            target_mpm=target_mpm,
        )
        return packed or []

    def _pack_leftovers(
        self,
        leftovers: List[Dict],
        pallet_dims: Dict,
        target_mpm: Optional[float],
    ) -> List[List[Dict]]:
        """把池中剩余箱子打成尽量多的托盘（不要求达标）。"""
        result: List[List[Dict]] = []
        pool = list(leftovers)
        seed = 7001
        no_progress = 0
        while pool and no_progress < 2:
            packer = self._CustomPacker(
                pallet_dims,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                size_tolerance=2.0,
                max_candidate_points=120,
                max_points_per_layer=25,
                constraint_config=self._cfg,
            )
            packed, _ = packer.pack(
                pool,
                num_restarts=3,
                beam_width=2,
                candidate_limit=6,
                random_seed=seed,
                target_mpm=target_mpm,
            )
            seed += 13
            if not packed:
                no_progress += 1
                continue
            used = {b['id'] for b in packed}
            new_pool = [b for b in pool if b['id'] not in used]
            if len(new_pool) == len(pool):
                no_progress += 1
                continue
            no_progress = 0
            pool = new_pool
            result.append(packed)
        return result

    def _apply_replacement(
        self,
        type_plans: List[Dict],
        original_failed: List[Dict],
        new_success_pallets: List[List[Dict]],
        leftover_pallets: List[List[Dict]],
        pallet_dims: Dict,
        target_mpm: float,
    ) -> int:
        """用新盘替换原失败盘。返回挽回数（=新 SUCCESS 盘数）。"""
        # 从 type_plans 中删除所有原失败盘
        failed_ids = {id(p) for p in original_failed}
        kept = [p for p in type_plans if id(p) not in failed_ids]

        # 取第一个失败盘做模板（pallet_type / sales_order_no 一致）
        template = original_failed[0]
        pallet_type = template['pallet_type']
        sales_order_no = template['sales_order_no']
        # 重新分配 pallet_id 序号（在原序号后追加）
        existing_ids = [p['pallet_id'] for p in type_plans]

        def _next_id() -> str:
            n = len(existing_ids) + 1
            new_id = f"{pallet_type}-{sales_order_no}-R{n}"
            existing_ids.append(new_id)
            return new_id

        rescued = len(new_success_pallets)
        candidate_plans = list(kept)
        for packed in new_success_pallets + leftover_pallets:
            solution = {
                "pallet_id": _next_id(),
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_target": target_mpm,
            }
            PalletEvaluator.calc_pallet_status(solution)

            com = self._validate_com(solution, pallet_dims)
            solution['stability_checks'] = {}
            if not com.get('is_stable', True):
                solution['stability_checks']['center_of_mass_failure'] = com
                solution['stability_checks']['status'] = 'FAILED'
            else:
                solution['stability_checks']['status'] = 'SUCCESS'

            candidate_plans.append(solution)

        gate = validate_plan_constraints(
            candidate_plans, pallet_dims, constraint_config=self._cfg
        )
        if not gate["is_valid"]:
            return -1

        type_plans.clear()
        type_plans.extend(candidate_plans)
        return rescued
