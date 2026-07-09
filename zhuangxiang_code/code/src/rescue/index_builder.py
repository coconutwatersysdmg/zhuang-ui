"""
索引构建器

负责构建装箱索引，包括诊断信息、候选池和指数桶。
"""

from typing import Dict, List, Optional
import random
import math


class IndexBuilder:
    """
    索引构建器

    构建装箱索引，用于优化装箱策略。
    """

    @staticmethod
    def build_index_diagnostics(
        items: List[Dict],
        target_mpm: Optional[float],
        pallet_dims: Optional[Dict[str, float]] = None
    ) -> Dict:
        """
        统计订单组在指数维度上的理论达标上限

        Args:
            items: 待装箱物品列表
            target_mpm: 目标MPM值
            pallet_dims: 托盘尺寸（可选）

        Returns:
            诊断信息字典，包含：
                - total_mpm: 总MPM值
                - theoretical_success_pallets: 理论成功托盘数
                - residual_mpm: 剩余MPM值
                - box_count: 箱子总数
                - positive_mpm_box_count: 正MPM箱子数
                - max_box_mpm: 最大箱子MPM值
                - top_10_box_mpm_sum: 前10个箱子MPM总和
                - canonical_layer_best: 典型层最佳MPM估算

        Examples:
            >>> builder = IndexBuilder()
            >>> items = [
            ...     {'min_pack_multiple': 10.0},
            ...     {'min_pack_multiple': 20.0}
            ... ]
            >>> diag = builder.build_index_diagnostics(items, 25.0)
            >>> diag['total_mpm']
            30.0
            >>> diag['theoretical_success_pallets']
            1
        """
        from .pallet_evaluator import PalletEvaluator

        total_mpm = sum(
            float(b.get('min_pack_multiple', 0) or 0)
            for b in items
        )

        positive_mpm_values = sorted(
            [
                float(b.get('min_pack_multiple', 0) or 0)
                for b in items
                if float(b.get('min_pack_multiple', 0) or 0) > 0
            ],
            reverse=True
        )

        canonical_layer_best = None
        if pallet_dims:
            canonical_layer_best = PalletEvaluator.estimate_canonical_layer_best_mpm(
                items, pallet_dims
            )

        if target_mpm is None or target_mpm <= 0:
            return {
                "total_mpm": total_mpm,
                "theoretical_success_pallets": None,
                "residual_mpm": None,
                "box_count": len(items),
                "positive_mpm_box_count": len(positive_mpm_values),
                "canonical_layer_best": canonical_layer_best
            }

        theoretical_success = int(total_mpm // target_mpm)
        residual_mpm = total_mpm - theoretical_success * target_mpm

        return {
            "total_mpm": total_mpm,
            "theoretical_success_pallets": theoretical_success,
            "residual_mpm": residual_mpm,
            "box_count": len(items),
            "positive_mpm_box_count": len(positive_mpm_values),
            "max_box_mpm": positive_mpm_values[0] if positive_mpm_values else 0.0,
            "top_10_box_mpm_sum": sum(positive_mpm_values[:10]),
            "canonical_layer_best": canonical_layer_best
        }

    @staticmethod
    def build_index_bucket(
        items: List[Dict],
        target_mpm: Optional[float],
        seed: int = 0,
        max_overflow: float = 8.0,
        max_sample_size: int = 180
    ) -> List[Dict]:
        """
        用小规模背包搜索构造一个接近目标指数的箱子桶

        该桶只解决指数组合问题，后续仍由CustomPacker验证几何、稳定性和机械臂可达性。

        Args:
            items: 待装箱物品列表
            target_mpm: 目标MPM值
            seed: 随机种子
            max_overflow: 最大溢出量
            max_sample_size: 最大样本大小

        Returns:
            接近目标指数的箱子列表

        Examples:
            >>> builder = IndexBuilder()
            >>> items = [
            ...     {'id': 1, 'min_pack_multiple': 10.0, 'length': 100, 'width': 100, 'height': 100},
            ...     {'id': 2, 'min_pack_multiple': 15.0, 'length': 100, 'width': 100, 'height': 100}
            ... ]
            >>> bucket = builder.build_index_bucket(items, 20.0, seed=42)
            >>> len(bucket) >= 0
            True
        """
        if not items or target_mpm is None or target_mpm <= 0:
            return []

        rng = random.Random(seed)
        items_copy = list(items)
        rng.shuffle(items_copy)

        def _mpm(box):
            return float(box.get('min_pack_multiple', 0) or 0)

        def _volume(box):
            return max(
                float(box['length'] * box['width'] * box['height']),
                1.0
            )

        # 筛选正MPM物品
        positive_items = [b for b in items_copy if _mpm(b) > 0]
        if not positive_items:
            return []

        # 构建样本池
        by_mpm = sorted(
            positive_items,
            key=lambda b: (-_mpm(b), _volume(b))
        )
        by_density = sorted(
            positive_items,
            key=lambda b: (-_mpm(b) / _volume(b), -_mpm(b))
        )

        sample = []
        seen_ids = set()

        for pool, limit in (
            (by_mpm, 100),
            (by_density, 60),
            (positive_items, 40)
        ):
            if pool is positive_items:
                pool = list(pool)
                rng.shuffle(pool)

            added_from_pool = 0
            for box in pool:
                if box['id'] in seen_ids:
                    continue
                sample.append(box)
                seen_ids.add(box['id'])
                added_from_pool += 1

                if len(sample) >= max_sample_size:
                    break
                if added_from_pool >= limit:
                    break

            if len(sample) >= max_sample_size:
                break

        # 动态规划求解背包问题
        scale = 10
        target_int = int(math.ceil(target_mpm * scale - 1e-9))
        max_sum = int(math.floor((target_mpm + max_overflow) * scale + 1e-9))
        dp = {0: (0, 0.0, [])}

        for box in sample:
            value = int(round(_mpm(box) * scale))
            if value <= 0 or value > max_sum:
                continue

            box_volume = _volume(box)
            for current_sum, (count, volume, combo) in list(dp.items()):
                new_sum = current_sum + value
                if new_sum > max_sum:
                    continue

                new_key = (count + 1, volume + box_volume)
                old = dp.get(new_sum)
                if old is None or new_key < (old[0], old[1]):
                    dp[new_sum] = (
                        count + 1,
                        volume + box_volume,
                        combo + [box]
                    )

        # 选择最佳组合
        best = None
        for total_int, (count, volume, combo) in dp.items():
            if total_int < target_int or not combo:
                continue

            overflow = total_int - target_int
            key = (overflow, count, volume)
            if best is None or key < best[0]:
                best = (key, combo)

        return list(best[1]) if best else []

    @staticmethod
    def build_index_candidate_pool(
        items: List[Dict],
        target_mpm: Optional[float],
        seed: int = 0,
        expand_factor: float = 1.0
    ) -> List[Dict]:
        """
        构建智能候选箱子池

        从所有可用箱子中筛选出适合当前托盘的候选集合，平衡MPM目标达成和几何可行性。
        采用两阶段选择策略：首先选择接近目标指数的箱子避免过早透支高MPM箱，然后补充
        少量候选增加几何排列的灵活性。

        Args:
            items: 所有可用箱子列表
            target_mpm: 目标MPM值，为None时返回所有箱子
            seed: 随机种子，用于控制箱子洗牌顺序
            expand_factor: 扩展因子，控制候选池规模放大倍数，默认1.0

        Returns:
            筛选后的候选箱子列表，规模自适应控制在合理范围内

        Examples:
            >>> builder = IndexBuilder()
            >>> items = [
            ...     {'id': i, 'min_pack_multiple': float(i)}
            ...     for i in range(1, 11)
            ... ]
            >>> pool = builder.build_index_candidate_pool(items, 20.0, seed=42)
            >>> len(pool) > 0
            True
        """
        if not items:
            return []
        if target_mpm is None:
            return list(items)

        rng = random.Random(seed)
        items_copy = list(items)
        rng.shuffle(items_copy)

        # 先选一批"接近目标指数"的箱子，避免过早透支高mpm箱
        sorted_by_mpm = sorted(
            items_copy,
            key=lambda b: b.get('min_pack_multiple', 0),
            reverse=True
        )

        selected = []
        selected_ids = set()
        running_mpm = 0.0
        effective_target = target_mpm * max(1.0, expand_factor)

        for box in sorted_by_mpm:
            box_mpm = box.get('min_pack_multiple', 0)
            if (
                running_mpm < effective_target or
                box_mpm <= max(target_mpm * 0.1, 1)
            ):
                selected.append(box)
                selected_ids.add(box['id'])
                running_mpm += box_mpm

            if running_mpm >= effective_target:
                break

        remaining = [b for b in items_copy if b['id'] not in selected_ids]

        # 补充少量候选增加几何可行性，规模自适应控制
        base_pool_size = max(60, int(len(items_copy) * 0.2))
        max_pool_size = min(
            len(items_copy),
            int(base_pool_size * expand_factor) + 40
        )

        if len(selected) < max_pool_size:
            need = max_pool_size - len(selected)
            selected.extend(remaining[:need])

        return selected

    @staticmethod
    def build_pallet_candidate_pool(
        items: List[Dict],
        target_mpm: Optional[float],
        seed: int = 0,
        pallet_dims: Optional[Dict[str, float]] = None,
        max_items: int = 96,
        fill_ratio: float = 0.45,
    ) -> List[Dict]:
        """Build the main packing pool from index, fill, and tail-protection goals."""
        if not items:
            return []
        if len(items) <= max_items:
            return list(items)

        selected: List[Dict] = []
        selected_ids = set()

        def _add(pool: List[Dict], limit: Optional[int] = None) -> None:
            for box in pool:
                box_id = box.get('id')
                if box_id in selected_ids:
                    continue
                selected.append(box)
                selected_ids.add(box_id)
                if len(selected) >= max_items:
                    return
                if limit is not None and len(selected) >= limit:
                    return

        if target_mpm is not None and target_mpm > 0:
            index_bucket = IndexBuilder.build_index_bucket(
                items,
                target_mpm,
                seed=seed,
                max_overflow=max(24.0, target_mpm * 0.18),
                max_sample_size=max_items,
            )
            _add(index_bucket)
            if sum(float(b.get('min_pack_multiple', 0) or 0) for b in selected) < target_mpm:
                expanded = IndexBuilder.build_index_candidate_pool(
                    items, target_mpm, seed=seed, expand_factor=1.6
                )
                _add(expanded, limit=max(24, int(max_items * 0.55)))

        fill_target = max(len(selected) + 1, int(max_items * fill_ratio))
        fill_pool = IndexBuilder._build_fill_friendly_pool(
            items, pallet_dims, seed
        )
        _add(fill_pool, limit=fill_target)

        if target_mpm is not None and target_mpm > 0:
            remaining = [b for b in items if b.get('id') not in selected_ids]
            remaining_mpm = sum(
                float(b.get('min_pack_multiple', 0) or 0) for b in remaining
            )
            tail_floor = target_mpm * 0.35
            if 0 < remaining_mpm < tail_floor:
                low_mpm = sorted(
                    remaining,
                    key=lambda b: (
                        float(b.get('min_pack_multiple', 0) or 0),
                        -IndexBuilder._box_volume(b),
                        str(b.get('id')),
                    ),
                )
                _add(low_mpm, limit=min(max_items, len(selected) + 12))

        if len(selected) < max_items:
            fallback = sorted(
                [b for b in items if b.get('id') not in selected_ids],
                key=lambda b: (
                    -IndexBuilder._box_volume(b),
                    -float(b.get('min_pack_multiple', 0) or 0),
                    str(b.get('id')),
                ),
            )
            _add(fallback)

        return selected[:max_items]

    @staticmethod
    def _build_fill_friendly_pool(
        items: List[Dict],
        pallet_dims: Optional[Dict[str, float]],
        seed: int,
    ) -> List[Dict]:
        rng = random.Random(seed + 911)
        pallet_length = float((pallet_dims or {}).get('length', 0) or 0)
        pallet_width = float((pallet_dims or {}).get('width', 0) or 0)
        pallet_height = float((pallet_dims or {}).get('height', 0) or 0)
        groups: Dict[tuple, List[Dict]] = {}
        for box in items:
            key = (
                box.get('type'),
                float(box.get('length', 0) or 0),
                float(box.get('width', 0) or 0),
                float(box.get('height', 0) or 0),
                float(box.get('min_pack_multiple', 0) or 0),
            )
            groups.setdefault(key, []).append(box)

        def _group_score(entry):
            (_, length, width, height, mpm), group = entry
            eff_l = length + 2.0
            eff_w = width + 2.0
            eff_h = height
            if (
                eff_l <= 0 or eff_w <= 0 or eff_h <= 0
                or pallet_length <= 0 or pallet_width <= 0 or pallet_height <= 0
            ):
                per_pallet = len(group)
            else:
                per_layer = int(pallet_length // eff_l) * int(pallet_width // eff_w)
                layers = int(pallet_height // eff_h)
                per_pallet = max(0, per_layer * layers)
            usable = min(len(group), per_pallet if per_pallet > 0 else len(group))
            return (
                usable * length * width * height,
                usable,
                len(group),
                mpm,
            )

        ordered_groups = sorted(
            groups.items(),
            key=lambda entry: (*_group_score(entry), str(entry[0][0])),
            reverse=True,
        )
        result: List[Dict] = []
        for _, group in ordered_groups:
            group = list(group)
            rng.shuffle(group)
            group.sort(
                key=lambda b: (
                    -IndexBuilder._box_volume(b),
                    -float(b.get('min_pack_multiple', 0) or 0),
                    str(b.get('id')),
                )
            )
            result.extend(group)
        return result

    @staticmethod
    def _box_volume(box: Dict) -> float:
        return (
            float(box.get('length', 0) or 0)
            * float(box.get('width', 0) or 0)
            * float(box.get('height', 0) or 0)
        )

    @staticmethod
    def build_index_bucket_candidate_pool(
        items: List[Dict],
        target_mpm: Optional[float],
        seed: int = 0,
        expand_factor: float = 1.0,
        pallet_dims: Optional[Dict[str, float]] = None
    ) -> List[Dict]:
        """
        构建基于桶的候选池

        结合背包搜索和候选池扩展，构建更优的候选集合。

        Args:
            items: 所有可用箱子列表
            target_mpm: 目标MPM值
            seed: 随机种子
            expand_factor: 扩展因子
            pallet_dims: 托盘尺寸（可选）

        Returns:
            候选箱子列表

        Examples:
            >>> builder = IndexBuilder()
            >>> items = [
            ...     {'id': i, 'min_pack_multiple': float(i), 'length': 100, 'width': 100, 'height': 100}
            ...     for i in range(1, 11)
            ... ]
            >>> pool = builder.build_index_bucket_candidate_pool(items, 20.0, seed=42)
            >>> len(pool) > 0
            True
        """
        if not items or target_mpm is None:
            return list(items) if items else []

        # 先用背包搜索构建核心桶
        bucket = IndexBuilder.build_index_bucket(
            items, target_mpm, seed=seed
        )

        if not bucket:
            return IndexBuilder.build_index_candidate_pool(
                items, target_mpm, seed=seed, expand_factor=expand_factor
            )

        bucket_ids = {b['id'] for b in bucket}
        remaining = [b for b in items if b['id'] not in bucket_ids]

        extra_count = int(len(bucket) * expand_factor)
        if extra_count > 0 and remaining:
            rng = random.Random(seed + 1)
            rng.shuffle(remaining)
            bucket.extend(remaining[:extra_count])

        return bucket

    @staticmethod
    def _apply_density_quota(*args, **kwargs):
        """已验证无效，保留空 stub 以兼容潜在外部引用。"""
        return list(args[0]) if args else []

    @staticmethod
    def _apply_density_quota(*args, **kwargs):
        """已验证无效，保留空 stub 以兼容潜在外部引用。"""
        return list(args[0]) if args else []
