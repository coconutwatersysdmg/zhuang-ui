"""
失败盘洞填充救援

保留 receiver 现有摆放，把 donor 失败盘的箱子塞进可行空位。提取自原
zhuangxiang.fast_rescue_failed_pallets_by_hole_fill。
"""

import random
from copy import deepcopy
from typing import Dict, List, Optional

from src.geometry.center_of_mass import refresh_pallet_stability_status
from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.beam_search_packer import BeamSearchPacker
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import item_volume, sum_item_mpm


def _refresh_index_status(plan: Dict, target_mpm: Optional[float]) -> None:
    plan['mpm_target'] = target_mpm
    PalletEvaluator.calc_pallet_status(plan)


def fast_rescue_failed_pallets_by_hole_fill(
    type_plans: List[Dict],
    pallet_dims: Dict[str, float],
    target_mpm: Optional[float],
    max_gap: float = 64.0,
    max_attempts: int = 80,
    max_donor_scan: int = 160,
    max_add_items: int = 8,
    seed_base: int = 41000,
    constraint_config=None,
) -> Dict:
    """保留 receiver 现状，向其空位塞 donor 箱子。"""
    if constraint_config is None:
        from src.config.constraint_config import ConstraintConfig
        constraint_config = ConstraintConfig()
    diag = {
        "rescued": 0,
        "hole_fill_tried": 0,
        "hole_fill_success": 0,
        "hole_fill_no_candidate": 0,
        "hole_fill_pack_fail": 0,
        "hole_fill_max_gap": max_gap,
        "hole_fill_max_attempts": max_attempts,
    }
    if target_mpm is None or not type_plans or not pallet_dims:
        return diag

    for plan in type_plans:
        _refresh_index_status(plan, target_mpm)

    def _gap(plan: Dict) -> float:
        return max(0.0, float(plan.get('mpm_gap') or 0.0))

    def _rank_donor_item(item, need, receiver_gap, donor_gap):
        item_mpm = float(item.get('min_pack_multiple', 0) or 0)
        vol = item_volume(item)
        ratio = item_mpm / max(need, 1.0)
        return (
            abs(need - item_mpm),
            abs(receiver_gap - item_mpm),
            abs(donor_gap - item_mpm),
            -ratio,
            vol,
            -item_mpm,
            str(item.get('id')),
        )

    def _try_fill_receiver(receiver, donor_items, seed):
        rng = random.Random(seed)
        packer = BeamSearchPacker(
            pallet_dims,
            support_ratio_threshold=constraint_config.support_ratio_threshold,
            size_tolerance=0.0,
            z_tolerance=0.0,
            max_candidate_points=240,
            max_points_per_layer=80,
            constraint_config=constraint_config,
        )
        placed = [deepcopy(item) for item in receiver.get('packed_items', [])]
        used_items = []
        used_ids = set()

        for _ in range(max_add_items):
            need = target_mpm - sum_item_mpm(placed)
            if need <= 1e-9:
                break
            ranked_items = sorted(
                [
                    item for item in donor_items
                    if item.get('id') not in used_ids
                    and float(item.get('min_pack_multiple', 0) or 0) > 0
                ],
                key=lambda item: _rank_donor_item(
                    item, need, _gap(receiver), need
                ),
            )[:max_donor_scan]

            feasible_choices = []
            for item in ranked_items:
                candidates = packer._generate_feasible_candidates(
                    item, placed, rng
                )
                if not candidates:
                    continue
                best_candidate = sorted(
                    candidates, key=lambda c: c['score']
                )[0]
                box = best_candidate['box']
                projected_need = (
                    need - float(item.get('min_pack_multiple', 0) or 0)
                )
                feasible_choices.append((
                    abs(projected_need),
                    -float(item.get('min_pack_multiple', 0) or 0),
                    best_candidate['score'],
                    item,
                    box,
                ))

            if not feasible_choices:
                break

            feasible_choices.sort(key=lambda x: (x[0], x[1], x[2]))
            _, _, _, item, chosen = feasible_choices[0]
            placed.append(chosen)
            used_items.append(item)
            used_ids.add(item.get('id'))

            if sum_item_mpm(placed) >= target_mpm:
                break

        if sum_item_mpm(placed) >= target_mpm:
            return placed, used_items
        return [], []

    receivers = sorted(
        [
            plan for plan in type_plans
            if plan.get('mpm_status') == 'FAILED' and 0 < _gap(plan) <= max_gap
        ],
        key=lambda plan: (_gap(plan), len(plan.get('packed_items', []))),
    )

    for receiver in receivers:
        if diag["hole_fill_tried"] >= max_attempts:
            break
        if (
            receiver.get('mpm_status') != 'FAILED'
            or _gap(receiver) <= 0
            or _gap(receiver) > max_gap
        ):
            continue
        if not receiver.get('packed_items'):
            continue

        donor_items = []
        donor_by_id = {}
        for donor in type_plans:
            if donor is receiver or donor.get('mpm_status') != 'FAILED':
                continue
            for item in donor.get('packed_items', []):
                item_id = item.get('id')
                if item_id is None:
                    continue
                donor_items.append(item)
                donor_by_id[item_id] = donor

        if not donor_items:
            diag["hole_fill_no_candidate"] += 1
            continue

        diag["hole_fill_tried"] += 1
        packed, used_items = _try_fill_receiver(
            receiver,
            donor_items,
            seed_base + diag["hole_fill_tried"] + diag["rescued"] * 101,
        )
        if not packed or not used_items:
            diag["hole_fill_pack_fail"] += 1
            continue

        old_receiver_items = list(receiver.get('packed_items', []))
        old_donor_items = {}

        receiver['packed_items'] = packed
        _refresh_index_status(receiver, target_mpm)
        refresh_pallet_stability_status(receiver, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)

        changed_donor_ids = set()
        for used_item in used_items:
            used_id = used_item.get('id')
            donor = donor_by_id.get(used_id)
            if donor is None:
                continue
            old_donor_items.setdefault(id(donor), (donor, list(donor.get('packed_items', []))))
            donor['packed_items'] = [
                item for item in donor.get('packed_items', [])
                if item.get('id') != used_id
            ]
            changed_donor_ids.add(id(donor))
            _refresh_index_status(donor, target_mpm)

        for donor in type_plans:
            if id(donor) in changed_donor_ids:
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
            diag["hole_fill_pack_fail"] += 1
            continue

        diag["rescued"] += 1
        diag["hole_fill_success"] += 1

    return diag
