"""
快速失败盘补齐救援

对小缺口失败盘，从其他盘借少量箱子凑到目标 mpm。保守、低成本，但在密集
装箱场景下命中率低（receiver_ids 必须保留导致几何拒绝）。

提取自原 zhuangxiang.fast_rescue_failed_pallets_by_topup。
"""

from typing import Dict, List, Optional

from src.geometry.center_of_mass import refresh_pallet_stability_status
from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.beam_search_packer import BeamSearchPacker
from src.packing.direct_layer_packer import build_direct_layer_packing_solution
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import item_volume, sum_item_mpm


def _refresh_index_status(plan: Dict, target_mpm: Optional[float]) -> None:
    if target_mpm is None:
        plan['mpm_target'] = None
        PalletEvaluator.calc_pallet_status(plan)
    else:
        plan['mpm_target'] = target_mpm
        PalletEvaluator.calc_pallet_status(plan)


def fast_rescue_failed_pallets_by_topup(
    type_plans: List[Dict],
    pallet_dims: Dict[str, float],
    target_mpm: Optional[float],
    max_gap: float = 64.0,
    max_attempts: int = 80,
    max_donor_scan: int = 80,
    donor_mpm_slack: float = 16.0,
    seed_base: int = 31000,
    constraint_config=None,
) -> Dict:
    """用其他盘里的箱子做低成本补齐。"""
    if constraint_config is None:
        from src.config.constraint_config import ConstraintConfig
        constraint_config = ConstraintConfig()
    diag = {
        "rescued": 0,
        "topup_tried": 0,
        "topup_success": 0,
        "topup_no_candidate": 0,
        "topup_pack_fail": 0,
        "topup_rejected_missing_receiver": 0,
        "topup_max_gap": max_gap,
        "topup_max_attempts": max_attempts,
    }
    if target_mpm is None or not type_plans or not pallet_dims:
        return diag

    for plan in type_plans:
        _refresh_index_status(plan, target_mpm)

    def _gap(plan: Dict) -> float:
        return max(0.0, float(plan.get('mpm_gap') or 0.0))

    def _rank_donor_item(item, need, receiver_gap, donor_gap, donor_is_success=False):
        item_mpm = float(item.get('min_pack_multiple', 0) or 0)
        vol = item_volume(item)
        ratio = item_mpm / max(need, 1.0)
        donor_bias = 0 if donor_is_success else 1
        return (
            donor_bias,
            abs(need - item_mpm),
            abs(receiver_gap - item_mpm),
            abs(donor_gap - item_mpm),
            -ratio,
            vol,
            -item_mpm,
            str(item.get('id')),
        )

    def _pack_topup_pool(pool, seed):
        packed = build_direct_layer_packing_solution(
            pool,
            target_mpm=target_mpm,
            pallet_dims=pallet_dims,
            seed=seed,
            xy_tolerance=0.0,
            z_tolerance=0.0,
            candidate_count=12,
            constraint_config=constraint_config,
        )
        if packed:
            return packed

        packer = BeamSearchPacker(
            pallet_dims,
            support_ratio_threshold=constraint_config.support_ratio_threshold,
            size_tolerance=0.0,
            z_tolerance=0.0,
            max_candidate_points=120,
            max_points_per_layer=25,
            constraint_config=constraint_config,
        )
        packed, _ = packer.pack(
            pool,
            num_restarts=2,
            beam_width=2,
            candidate_limit=7,
            random_seed=seed,
            target_mpm=target_mpm,
            stop_when_target_met=True,
            allow_skip_items=True,
        )
        return packed

    def _receiver_priority(plan):
        gap_value = _gap(plan)
        item_count = len(plan.get('packed_items', []))
        total_mpm = float(plan.get('mpm_total') or 0.0)
        gap_ratio = gap_value / max(float(target_mpm or 1.0), 1.0)
        density = total_mpm / max(item_count, 1)
        return (
            round(gap_value, 4),
            round(gap_ratio, 4),
            -density,
            -item_count,
            -total_mpm,
        )

    receivers = sorted(
        [
            plan for plan in type_plans
            if plan.get('mpm_status') == 'FAILED' and 0 < _gap(plan) <= max_gap
        ],
        key=_receiver_priority,
    )

    for receiver in receivers:
        if diag["topup_tried"] >= max_attempts:
            break
        if receiver.get('mpm_status') != 'FAILED':
            continue
        receiver_items = list(receiver.get('packed_items', []))
        receiver_ids = {item.get('id') for item in receiver_items}
        if not receiver_items or None in receiver_ids:
            continue
        gap_value = _gap(receiver)
        if gap_value <= 0 or gap_value > max_gap:
            continue

        donor_entries = []
        for donor in type_plans:
            if donor is receiver:
                continue
            donor_status = donor.get('mpm_status')
            donor_gap = _gap(donor)
            donor_total = float(donor.get('mpm_total') or 0.0)
            if donor_status == 'FAILED' and donor_gap <= 0:
                continue
            if donor_status == 'SUCCESS' and donor_total <= target_mpm:
                continue
            for item in donor.get('packed_items', []):
                item_id = item.get('id')
                if item_id is None or item_id in receiver_ids:
                    continue
                item_mpm = float(item.get('min_pack_multiple', 0) or 0)
                if item_mpm <= 0:
                    continue
                donor_entries.append((
                    _rank_donor_item(
                        item,
                        gap_value,
                        gap_value,
                        donor_gap,
                        donor_status == 'SUCCESS',
                    ),
                    donor,
                    item,
                ))

        if not donor_entries:
            diag["topup_no_candidate"] += 1
            continue

        donor_entries.sort(key=lambda entry: entry[0])
        candidate_items = []
        donor_by_id = {}
        candidate_mpm = 0.0
        for _, donor, item in donor_entries[:max_donor_scan]:
            item_id = item.get('id')
            item_mpm = float(item.get('min_pack_multiple', 0) or 0)
            candidate_items.append(item)
            donor_by_id[item_id] = donor
            candidate_mpm += item_mpm
            if (
                candidate_mpm >= target_mpm
                and candidate_mpm >= gap_value + donor_mpm_slack
            ):
                break
            if (
                candidate_mpm >= gap_value + donor_mpm_slack
                and len(candidate_items) >= 18
            ):
                break
        if len(candidate_items) > 18:
            trimmed_items = candidate_items[:18]
            trimmed_mpm = sum_item_mpm(trimmed_items)
            if trimmed_mpm + 1e-9 >= gap_value:
                candidate_items = trimmed_items
            else:
                extended_items = []
                extended_mpm = 0.0
                cap = min(
                    len(candidate_items),
                    max(18, min(max_donor_scan, 40)),
                )
                for item in candidate_items[:cap]:
                    extended_items.append(item)
                    extended_mpm += float(item.get('min_pack_multiple', 0) or 0)
                    if extended_mpm + 1e-9 >= gap_value:
                        break
                candidate_items = extended_items
            candidate_mpm = sum_item_mpm(candidate_items)

        if candidate_mpm + 1e-9 < gap_value:
            diag["topup_no_candidate"] += 1
            continue

        diag["topup_tried"] += 1
        pool = receiver_items + candidate_items
        packed = _pack_topup_pool(
            pool,
            seed_base + diag["topup_tried"] + diag["rescued"] * 101,
        )
        packed_mpm = sum_item_mpm(packed)
        if packed_mpm + 1e-9 < target_mpm:
            diag["topup_pack_fail"] += 1
            continue

        packed_ids = {item.get('id') for item in packed}
        if not receiver_ids.issubset(packed_ids):
            diag["topup_rejected_missing_receiver"] += 1
            continue

        added_ids = packed_ids - receiver_ids
        if not added_ids:
            diag["topup_pack_fail"] += 1
            continue

        old_receiver_items = list(receiver.get('packed_items', []))
        old_donor_items = {}

        receiver['packed_items'] = packed
        _refresh_index_status(receiver, target_mpm)
        refresh_pallet_stability_status(receiver, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)

        changed_donors = set()
        for added_id in added_ids:
            donor = donor_by_id.get(added_id)
            if donor is None:
                continue
            old_donor_items.setdefault(
                id(donor), (donor, list(donor.get('packed_items', [])))
            )
            donor['packed_items'] = [
                item for item in donor.get('packed_items', [])
                if item.get('id') != added_id
            ]
            changed_donors.add(id(donor))
            _refresh_index_status(donor, target_mpm)

        for donor in type_plans:
            if id(donor) in changed_donors:
                refresh_pallet_stability_status(donor, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)

        changed_plans = [receiver] + [
            donor for donor, _ in old_donor_items.values()
        ]
        if not all(
            validate_pallet_constraints(
                plan, pallet_dims, constraint_config=constraint_config
            )["is_valid"]
            for plan in changed_plans
            if plan.get('packed_items')
        ):
            receiver['packed_items'] = old_receiver_items
            _refresh_index_status(receiver, target_mpm)
            refresh_pallet_stability_status(receiver, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)
            for donor, items in old_donor_items.values():
                donor['packed_items'] = items
                _refresh_index_status(donor, target_mpm)
                refresh_pallet_stability_status(donor, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)
            diag["topup_pack_fail"] += 1
            continue

        diag["rescued"] += 1
        diag["topup_success"] += 1

    return diag
