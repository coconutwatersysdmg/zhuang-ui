"""Absorb low-index tail fragments into existing pallets.

This post-process keeps existing receiver placement and tries to place top
items from low-index failed pallets into feasible holes on success pallets or
other failed pallets.  A move is accepted only when it does not reduce the
number of successful pallets and improves the low-load tail situation.
"""

from copy import deepcopy
import random
from typing import Dict, List, Optional, Tuple

from src.geometry.center_of_mass import refresh_pallet_stability_status
from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.beam_search_packer import BeamSearchPacker
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import has_box_above


class TailFragmentAbsorber:
    """Low-cost tail-fragment absorption pass."""

    def __init__(
        self,
        low_box_count: int = 5,
        low_mpm: float = 32.0,
        deep_gap: float = 160.0,
        max_attempts: int = 120,
        max_items_per_donor: int = 8,
        seed_base: int = 81000,
        constraint_config=None,
    ):
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._cfg = constraint_config
        self._low_box_count = low_box_count
        self._low_mpm = low_mpm
        self._deep_gap = deep_gap
        self._max_attempts = max_attempts
        self._max_items_per_donor = max_items_per_donor
        self._seed_base = seed_base

    def absorb(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
    ) -> Dict:
        diag = {
            "tail_absorb_tried": 0,
            "tail_absorb_success": 0,
            "tail_absorb_donor_emptied": 0,
            "tail_absorb_rejected": 0,
            "tail_absorb_pack_fail": 0,
            "tail_absorb_old_success": 0,
            "tail_absorb_new_success": 0,
            "tail_absorb_old_low_count": 0,
            "tail_absorb_new_low_count": 0,
        }
        if target_mpm is None or not type_plans or not pallet_dims:
            return diag

        self._refresh_all(type_plans, pallet_dims, target_mpm)
        diag["tail_absorb_old_success"] = self._success_count(type_plans)
        diag["tail_absorb_old_low_count"] = self._low_count(type_plans)

        rng = random.Random(self._seed_base)
        packer = BeamSearchPacker(
            pallet_dims,
            support_ratio_threshold=self._cfg.support_ratio_threshold,
            size_tolerance=0.0,
            z_tolerance=0.0,
            max_candidate_points=180,
            max_points_per_layer=60,
            constraint_config=self._cfg,
        )

        attempts = 0
        while attempts < self._max_attempts:
            donor = self._next_low_donor(type_plans)
            if donor is None:
                break
            move = self._try_absorb_from_donor(
                donor, type_plans, pallet_dims, target_mpm, packer, rng
            )
            attempts += 1
            diag["tail_absorb_tried"] += 1
            if move == "accepted":
                diag["tail_absorb_success"] += 1
                if not donor.get('packed_items'):
                    diag["tail_absorb_donor_emptied"] += 1
                continue
            if move == "pack_fail":
                diag["tail_absorb_pack_fail"] += 1
            else:
                diag["tail_absorb_rejected"] += 1
            donor["_tail_absorb_blocked"] = True

        for plan in type_plans:
            plan.pop("_tail_absorb_blocked", None)

        diag["tail_absorb_new_success"] = self._success_count(type_plans)
        diag["tail_absorb_new_low_count"] = self._low_count(type_plans)
        return diag

    def _try_absorb_from_donor(
        self,
        donor: Dict,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: float,
        packer: BeamSearchPacker,
        rng: random.Random,
    ) -> str:
        before_success = self._success_count(type_plans)
        before_low = self._low_count(type_plans)
        before_pallets = self._nonempty_count(type_plans)

        for item in self._candidate_items(donor):
            for receiver in self._receiver_order(type_plans, donor):
                placed = [deepcopy(box) for box in receiver.get('packed_items', [])]
                candidates = packer._generate_feasible_candidates(
                    item, placed, rng
                )
                if not candidates:
                    continue
                chosen = sorted(candidates, key=lambda c: c['score'])[0]['box']
                old_receiver_items = receiver.get('packed_items', [])
                old_donor_items = donor.get('packed_items', [])

                receiver['packed_items'] = placed + [chosen]
                donor['packed_items'] = [
                    box for box in old_donor_items
                    if box.get('id') != item.get('id')
                ]
                self._refresh_plan(receiver, pallet_dims, target_mpm)
                self._refresh_plan(donor, pallet_dims, target_mpm)

                after_success = self._success_count(type_plans)
                after_low = self._low_count(type_plans)
                after_pallets = self._nonempty_count(type_plans)
                gate_ok = all(
                    validate_pallet_constraints(
                        plan, pallet_dims, constraint_config=self._cfg
                    )["is_valid"]
                    for plan in (receiver, donor)
                    if plan.get('packed_items')
                )
                accepted = (
                    gate_ok
                    and after_success >= before_success
                    and (
                        after_success > before_success
                        or after_pallets < before_pallets
                        or after_low < before_low
                    )
                )
                if accepted:
                    return "accepted"

                receiver['packed_items'] = old_receiver_items
                donor['packed_items'] = old_donor_items
                self._refresh_plan(receiver, pallet_dims, target_mpm)
                self._refresh_plan(donor, pallet_dims, target_mpm)

        return "pack_fail"

    def _candidate_items(self, donor: Dict) -> List[Dict]:
        items = donor.get('packed_items', [])
        movable = [
            item for item in items
            if item.get('id') is not None
            and not has_box_above(item, items)
            and float(item.get('min_pack_multiple', 0) or 0) > 0
        ]
        movable.sort(
            key=lambda item: (
                -float(item.get('min_pack_multiple', 0) or 0),
                float(item.get('length', 0) or 0)
                * float(item.get('width', 0) or 0),
                str(item.get('id')),
            )
        )
        return movable[:self._max_items_per_donor]

    def _receiver_order(
        self,
        type_plans: List[Dict],
        donor: Dict,
    ) -> List[Dict]:
        receivers = [
            plan for plan in type_plans
            if plan is not donor and plan.get('packed_items')
        ]

        def _rank(plan: Dict) -> Tuple:
            status = plan.get('mpm_status')
            total = float(plan.get('mpm_total') or 0.0)
            gap = float(plan.get('mpm_gap') or 0.0)
            return (
                0 if status == 'SUCCESS' else 1,
                abs(gap),
                -total,
                len(plan.get('packed_items', [])),
                str(plan.get('pallet_id')),
            )

        return sorted(receivers, key=_rank)

    def _next_low_donor(self, type_plans: List[Dict]) -> Optional[Dict]:
        donors = [
            plan for plan in type_plans
            if not plan.get("_tail_absorb_blocked")
            and plan.get('mpm_status') == 'FAILED'
            and plan.get('packed_items')
            and self._is_low(plan)
        ]
        if not donors:
            return None
        return sorted(
            donors,
            key=lambda plan: (
                float(plan.get('mpm_total') or 0.0),
                len(plan.get('packed_items', [])),
                -float(plan.get('mpm_gap') or 0.0),
                str(plan.get('pallet_id')),
            ),
        )[0]

    def _is_low(self, plan: Dict) -> bool:
        return (
            len(plan.get('packed_items', [])) <= self._low_box_count
            or float(plan.get('mpm_total') or 0.0) < self._low_mpm
            or float(plan.get('mpm_gap') or 0.0) >= self._deep_gap
        )

    def _low_count(self, type_plans: List[Dict]) -> int:
        return sum(
            1 for plan in type_plans
            if plan.get('packed_items') and plan.get('mpm_status') == 'FAILED'
            and self._is_low(plan)
        )

    def _success_count(self, type_plans: List[Dict]) -> int:
        return sum(
            1 for plan in type_plans
            if plan.get('packed_items') and plan.get('mpm_status') == 'SUCCESS'
        )

    def _nonempty_count(self, type_plans: List[Dict]) -> int:
        return sum(1 for plan in type_plans if plan.get('packed_items'))

    def _refresh_all(
        self,
        type_plans: List[Dict],
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
    ) -> None:
        for plan in type_plans:
            self._refresh_plan(plan, pallet_dims, target_mpm)

    def _refresh_plan(
        self,
        plan: Dict,
        pallet_dims: Dict[str, float],
        target_mpm: Optional[float],
    ) -> None:
        plan['mpm_target'] = target_mpm
        PalletEvaluator.calc_pallet_status(plan)
        if plan.get('packed_items'):
            refresh_pallet_stability_status(plan, pallet_dims, tolerance=self._cfg.center_of_mass_tolerance)
