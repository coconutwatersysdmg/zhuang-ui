"""
托盘评估器

负责评估托盘装箱方案的优劣，计算MPM状态和统计指标。
"""

from typing import Dict, List, Optional, Tuple


class PalletEvaluator:
    """
    托盘评估器

    评估托盘装箱方案的优劣，计算MPM状态和统计指标。
    """

    @staticmethod
    def evaluate_pallet_solution(
        packed_items: List[Dict],
        remaining_unfitted: List[Dict],
        target_mpm: Optional[float]
    ) -> Tuple[Tuple, float, Optional[float], Optional[bool]]:
        """
        评估托盘装箱方案的优劣

        根据MPM目标和剩余未装箱物品数量计算方案评分，用于比较不同装箱策略的效果。
        评分采用多级元组比较机制，优先级从高到低依次为：是否达标、缺口绝对值、
        MPM总量、剩余物品数、总体积。

        Args:
            packed_items: 已装载的箱子列表
            remaining_unfitted: 剩余未装载的箱子列表
            target_mpm: 目标MPM值，若为None则退化为MPM总量优先策略

        Returns:
            包含四个元素的元组：
                - score: 方案评分元组，用于方案比较
                - total_mpm: 已装载箱子的MPM总量
                - mpm_gap: MPM缺口值(target_mpm - total_mpm)，target_mpm为None时为None
                - is_target_met: 是否达到MPM目标，target_mpm为None时为None

        Examples:
            >>> evaluator = PalletEvaluator()
            >>> packed = [{'min_pack_multiple': 10, 'length': 100, 'width': 100, 'height': 100}]
            >>> unfitted = []
            >>> score, total_mpm, gap, met = evaluator.evaluate_pallet_solution(packed, unfitted, 20.0)
            >>> total_mpm
            10
            >>> gap
            10.0
            >>> met
            False
        """
        total_mpm = sum(b.get('min_pack_multiple', 0) for b in packed_items)
        remaining_mpm = sum(
            b.get('min_pack_multiple', 0) for b in remaining_unfitted
        )
        total_volume = sum(
            b['length'] * b['width'] * b['height']
            for b in packed_items
        )

        if target_mpm is None:
            score = (
                total_volume,
                len(packed_items),
                total_mpm,
                -len(remaining_unfitted),
            )
            return score, total_mpm, None, None

        mpm_gap = target_mpm - total_mpm
        is_target_met = 1 if mpm_gap <= 0 else 0
        future_success = int(remaining_mpm // target_mpm) if target_mpm > 0 else 0
        remaining_tail = (
            remaining_mpm - future_success * target_mpm
            if target_mpm > 0 else 0.0
        )
        tail_floor = target_mpm * 0.35
        if remaining_mpm <= 1e-9 or remaining_tail >= tail_floor:
            tail_penalty = 0.0
        else:
            tail_penalty = -(tail_floor - remaining_tail)

        if is_target_met:
            overflow = max(0.0, total_mpm - target_mpm)
            overflow_allowance = max(16.0, target_mpm * 0.15)
            excess_overflow = max(0.0, overflow - overflow_allowance)
            score = (
                is_target_met,
                total_volume,
                len(packed_items),
                future_success,
                tail_penalty,
                -excess_overflow,
                total_mpm,
                -abs(mpm_gap),
                -len(remaining_unfitted),
            )
        else:
            score = (
                is_target_met,
                -abs(mpm_gap),
                total_mpm,
                total_volume,
                len(packed_items),
                future_success,
                tail_penalty,
                -len(remaining_unfitted),
            )
        return score, total_mpm, mpm_gap, bool(is_target_met)

    @staticmethod
    def calc_pallet_status(solution: Dict) -> str:
        """
        计算并更新托盘的MPM状态

        根据已装载箱子的MPM总量和目标值，计算缺口并判定状态。直接修改传入的
        solution字典，添加mpm_total、mpm_gap、mpm_status字段。

        Args:
            solution: 托盘方案字典，需包含'packed_items'和'mpm_target'字段

        Returns:
            托盘状态字符串
                - "SUCCESS": MPM总量达到或超过目标值
                - "FAILED": MPM总量低于目标值
                - "UNKNOWN": 未配置MPM目标

        Examples:
            >>> evaluator = PalletEvaluator()
            >>> solution = {
            ...     'packed_items': [{'min_pack_multiple': 10}],
            ...     'mpm_target': 20.0
            ... }
            >>> status = evaluator.calc_pallet_status(solution)
            >>> status
            'FAILED'
            >>> solution['mpm_total']
            10
            >>> solution['mpm_gap']
            10.0
        """
        target = solution.get('mpm_target')
        total = sum(
            b.get('min_pack_multiple', 0)
            for b in solution.get('packed_items', [])
        )
        gap = None if target is None else (target - total)
        status = (
            "UNKNOWN" if target is None else
            ("SUCCESS" if total >= target else "FAILED")
        )

        solution['mpm_total'] = total
        solution['mpm_gap'] = gap
        solution['mpm_status'] = status
        return status

    @staticmethod
    def recompute_type_stats(type_plans: List[Dict]) -> Dict:
        """
        重新计算某托盘类型下所有托盘的统计指标

        遍历该类型下的所有托盘方案，统计成功/失败/未知状态的托盘数量，
        并计算失败托盘的平均和最大MPM缺口。

        Args:
            type_plans: 某托盘类型的所有托盘方案列表

        Returns:
            统计结果字典，包含：
                - total_pallets: 总托盘数
                - success_pallets: 成功托盘数
                - failed_pallets: 失败托盘数
                - unknown_pallets: 未知状态托盘数
                - avg_mpm_gap: 失败托盘的平均MPM缺口（保留2位小数）
                - max_mpm_gap: 失败托盘的最大MPM缺口

        Examples:
            >>> evaluator = PalletEvaluator()
            >>> plans = [
            ...     {'packed_items': [{'min_pack_multiple': 10}], 'mpm_target': 20.0},
            ...     {'packed_items': [{'min_pack_multiple': 25}], 'mpm_target': 20.0}
            ... ]
            >>> stats = evaluator.recompute_type_stats(plans)
            >>> stats['total_pallets']
            2
            >>> stats['success_pallets']
            1
            >>> stats['failed_pallets']
            1
        """
        stats = {
            "total_pallets": len(type_plans),
            "success_pallets": 0,
            "failed_pallets": 0,
            "unknown_pallets": 0,
            "avg_mpm_gap": 0.0,
            "max_mpm_gap": 0.0
        }

        gap_sum = 0.0
        for p in type_plans:
            status = PalletEvaluator.calc_pallet_status(p)
            if status == "SUCCESS":
                stats["success_pallets"] += 1
            elif status == "FAILED":
                stats["failed_pallets"] += 1
                gap_value = max(0.0, float(p.get('mpm_gap') or 0.0))
                gap_sum += gap_value
                stats["max_mpm_gap"] = max(stats["max_mpm_gap"], gap_value)
            else:
                stats["unknown_pallets"] += 1

        if stats["failed_pallets"] > 0:
            stats["avg_mpm_gap"] = round(gap_sum / stats["failed_pallets"], 2)
        else:
            stats["avg_mpm_gap"] = 0.0

        return stats

    @staticmethod
    def estimate_canonical_layer_best_mpm(
        items: List[Dict],
        pallet_dims: Dict[str, float],
        size_tolerance: float = 2.0,
        z_tolerance: float = 0.0
    ) -> Dict:
        """
        估算典型整层堆叠模式下的单托盘最高指数

        这是一个诊断值，不替代完整三维装箱搜索。它用于快速识别：
        总指数足够，但在当前托盘高度、箱型高度和整层footprint下，目标指数可能不可达。

        Args:
            items: 待装箱物品列表
            pallet_dims: 托盘尺寸 {'length': float, 'width': float, 'height': float}
            size_tolerance: XY方向尺寸容差（毫米）
            z_tolerance: Z方向尺寸容差（毫米）

        Returns:
            估算结果字典，包含：
                - best_mpm: 最佳MPM值
                - best_layers: 最佳层配置列表

        Examples:
            >>> evaluator = PalletEvaluator()
            >>> items = [
            ...     {'type': 'A', 'length': 100, 'width': 100, 'height': 100, 'min_pack_multiple': 1.0}
            ... ] * 10
            >>> pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
            >>> result = evaluator.estimate_canonical_layer_best_mpm(items, pallet_dims)
            >>> result['best_mpm'] >= 0
            True
        """
        if not items or not pallet_dims:
            return {
                "best_mpm": 0.0,
                "best_layers": []
            }

        # 统计箱型数量
        type_counts = {}
        for item in items:
            key = (
                item.get('type'),
                float(item.get('length', 0) or 0),
                float(item.get('width', 0) or 0),
                float(item.get('height', 0) or 0),
                float(item.get('min_pack_multiple', 0) or 0)
            )
            type_counts[key] = type_counts.get(key, 0) + 1

        # 计算每种箱型的层选项
        layer_options = []
        pallet_length = float(pallet_dims.get('length', 0) or 0)
        pallet_width = float(pallet_dims.get('width', 0) or 0)
        pallet_height = float(pallet_dims.get('height', 0) or 0)

        for (box_type, length, width, height, mpm), count in type_counts.items():
            if length <= 0 or width <= 0 or height <= 0 or mpm <= 0:
                continue

            effective_length = length + size_tolerance
            effective_width = width + size_tolerance
            effective_height = height + z_tolerance

            if effective_height > pallet_height + 1e-9:
                continue

            # 计算每层可放置的箱子数量
            per_layer_count = (
                int(pallet_length // effective_length) *
                int(pallet_width // effective_width)
            )

            if per_layer_count <= 0:
                continue

            usable_count = min(count, per_layer_count)
            layer_options.append({
                "box_type": box_type,
                "layer_height": effective_height,
                "layer_mpm": usable_count * mpm,
                "box_count": usable_count,
                "box_mpm": mpm,
                "box_dims": {
                    "length": length,
                    "width": width,
                    "height": height
                }
            })

        if not layer_options:
            return {
                "best_mpm": 0.0,
                "best_layers": []
            }

        # 使用贪心算法选择最佳层组合
        height_scale = 10000  # 高度缩放因子
        layer_options.sort(
            key=lambda x: (
                -x["layer_mpm"],
                x["layer_height"] * height_scale
            )
        )

        best_layers = []
        remaining_height = pallet_height
        total_mpm = 0.0

        for layer in layer_options:
            if layer["layer_height"] <= remaining_height + 1e-9:
                best_layers.append(layer)
                remaining_height -= layer["layer_height"]
                total_mpm += layer["layer_mpm"]

                if remaining_height < 1e-9:
                    break

        return {
            "best_mpm": total_mpm,
            "best_layers": best_layers
        }
