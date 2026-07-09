"""
配方重建救援

从当前 SUCCESS 盘提取 (type, L, W, H, mpm)-count 配方，在剩余 inventory
中精确复制并验证。只有当新方案的 SUCCESS 数严格增加时才接受。
提取自原 zhuangxiang.rescue_by_recipe_rebuild。
"""

from typing import Dict, List, Optional

from src.geometry.center_of_mass import refresh_pallet_stability_status
from src.geometry.constraint_validator import validate_plan_constraints
from src.packing.beam_search_packer import BeamSearchPacker
from src.packing.direct_layer_packer import build_direct_layer_packing_solution
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import sum_item_mpm


def _refresh_index_status(plan: Dict, target_mpm: Optional[float]) -> None:
    plan['mpm_target'] = target_mpm
    PalletEvaluator.calc_pallet_status(plan)


def rescue_by_recipe_rebuild(
    type_plans: List[Dict],
    pallet_dims: Dict[str, float],
    target_mpm: Optional[float],
    max_group_boxes: int = 400,
    max_recipe_count: int = 12,
    seed_base: int = 51000,
    constraint_config=None,
) -> Dict:
    """复制 SUCCESS 盘配方到剩余 inventory；严格不变差才接受。"""
    if constraint_config is None:
        from src.config.constraint_config import ConstraintConfig
        constraint_config = ConstraintConfig()
    diag = {
        "rescued": 0,
        "recipe_rebuild_tried": 0,
        "recipe_rebuild_success": 0,
        "recipe_rebuild_skipped": 0,
        "recipe_rebuild_recipes": 0,
        "recipe_rebuild_generated_success": 0,
        "recipe_rebuild_old_success": 0,
        "recipe_rebuild_new_success": 0,
        "duplicates": 0,
    }
    if target_mpm is None or not type_plans or not pallet_dims:
        diag["recipe_rebuild_skipped"] = 1
        return diag

    for plan in type_plans:
        _refresh_index_status(plan, target_mpm)

    all_items = [
        item for plan in type_plans for item in plan.get('packed_items', [])
    ]
    if len(all_items) == 0 or len(all_items) > max_group_boxes:
        diag["recipe_rebuild_skipped"] = 1
        return diag

    old_success = sum(
        1 for plan in type_plans if plan.get('mpm_status') == 'SUCCESS'
    )
    diag["recipe_rebuild_old_success"] = old_success

    def _signature(item: Dict) -> tuple:
        return (
            str(item.get('type')),
            float(item.get('length', 0) or 0),
            float(item.get('width', 0) or 0),
            float(item.get('height', 0) or 0),
            float(item.get('min_pack_multiple', 0) or 0),
        )

    def _recipe_mpm(recipe: Dict) -> float:
        return sum(sig[4] * count for sig, count in recipe.items())

    recipes: List[Dict] = []
    seen_recipes = set()
    for plan in type_plans:
        if plan.get('mpm_status') != 'SUCCESS':
            continue
        recipe: Dict[tuple, int] = {}
        for item in plan.get('packed_items', []):
            sig = _signature(item)
            recipe[sig] = recipe.get(sig, 0) + 1
        if not recipe or abs(_recipe_mpm(recipe) - target_mpm) > 1e-9:
            continue
        recipe_key = tuple(sorted(recipe.items()))
        if recipe_key in seen_recipes:
            continue
        seen_recipes.add(recipe_key)
        recipes.append(recipe)

    if not recipes:
        diag["recipe_rebuild_skipped"] = 1
        return diag

    recipes.sort(
        key=lambda recipe: (
            sum(recipe.values()),
            len(recipe),
            tuple(sorted(recipe.items())),
        )
    )
    recipes = recipes[:max_recipe_count]
    diag["recipe_rebuild_recipes"] = len(recipes)
    diag["recipe_rebuild_tried"] = 1

    inventory: Dict[tuple, List[Dict]] = {}
    for item in all_items:
        inventory.setdefault(_signature(item), []).append(item)
    for items in inventory.values():
        items.sort(key=lambda item: str(item.get('id')))

    def _pack_exact_recipe(recipe: Dict, seed: int) -> List[Dict]:
        subset: List[Dict] = []
        for sig, count in recipe.items():
            available = inventory.get(sig, [])
            if len(available) < count:
                return []
            subset.extend(available[:count])

        packer = BeamSearchPacker(
            pallet_dims,
            support_ratio_threshold=constraint_config.support_ratio_threshold,
            size_tolerance=0.0,
            z_tolerance=0.0,
            max_candidate_points=240,
            max_points_per_layer=80,
            constraint_config=constraint_config,
        )
        packed, _ = packer.pack(
            subset,
            num_restarts=4,
            beam_width=2,
            candidate_limit=10,
            random_seed=seed,
            target_mpm=target_mpm,
            stop_when_target_met=False,
            allow_skip_items=False,
        )
        if {item.get('id') for item in packed} != {
            item.get('id') for item in subset
        }:
            return []
        if sum_item_mpm(packed) + 1e-9 < target_mpm:
            return []
        return packed

    generated_pallets: List[List[Dict]] = []
    seed = seed_base
    while True:
        packed_recipe: List[Dict] = []
        packed_recipe_sig_counts = None
        for recipe in recipes:
            packed_recipe = _pack_exact_recipe(recipe, seed)
            seed += 1
            if packed_recipe:
                packed_recipe_sig_counts = recipe
                break
        if not packed_recipe:
            break
        generated_pallets.append(packed_recipe)
        for sig, count in packed_recipe_sig_counts.items():
            del inventory[sig][:count]

    diag["recipe_rebuild_generated_success"] = len(generated_pallets)
    if len(generated_pallets) <= old_success:
        return diag

    remaining_items: List[Dict] = []
    for items in inventory.values():
        remaining_items.extend(items)
    remaining_items.sort(key=lambda item: str(item.get('id')))

    residual_pallets: List[List[Dict]] = []
    residual_counter = 1
    while remaining_items:
        packed_items = build_direct_layer_packing_solution(
            remaining_items,
            target_mpm=target_mpm,
            pallet_dims=pallet_dims,
            seed=seed_base + 1000 + residual_counter,
            xy_tolerance=0.0,
            z_tolerance=0.0,
            candidate_count=12,
            constraint_config=constraint_config,
        )
        if not packed_items:
            packer = BeamSearchPacker(
                pallet_dims,
                support_ratio_threshold=constraint_config.support_ratio_threshold,
                size_tolerance=0.0,
                max_candidate_points=120,
                max_points_per_layer=25,
                constraint_config=constraint_config,
            )
            packed_items, _ = packer.pack(
                remaining_items,
                num_restarts=4,
                beam_width=2,
                candidate_limit=7,
                random_seed=seed_base + 2000 + residual_counter,
                target_mpm=target_mpm,
                stop_when_target_met=True,
                allow_skip_items=True,
            )
        if not packed_items:
            return diag
        residual_pallets.append(packed_items)
        used_ids = {item.get('id') for item in packed_items}
        remaining_items = [
            item for item in remaining_items
            if item.get('id') not in used_ids
        ]
        residual_counter += 1

    rebuilt_item_sets = generated_pallets + residual_pallets
    new_success = sum(
        1 for items in rebuilt_item_sets
        if sum_item_mpm(items) + 1e-9 >= target_mpm
    )
    diag["recipe_rebuild_new_success"] = new_success
    if new_success <= old_success:
        return diag

    rebuilt_ids = [
        item.get('id') for items in rebuilt_item_sets for item in items
    ]
    diag["duplicates"] = len(rebuilt_ids) - len(set(rebuilt_ids))
    if diag["duplicates"] != 0 or len(rebuilt_ids) != len(all_items):
        return diag

    pallet_type = type_plans[0].get('pallet_type', 'UNKNOWN')
    sales_order_no = type_plans[0].get('sales_order_no', 'UNKNOWN_ORDER')
    candidate_plans: List[Dict] = []
    for idx, packed_items in enumerate(rebuilt_item_sets, start=1):
        plan = {
            "pallet_id": f"{pallet_type}-{sales_order_no}-{idx}",
            "pallet_type": pallet_type,
            "sales_order_no": sales_order_no,
            "packed_items": packed_items,
            "mpm_target": target_mpm,
        }
        _refresh_index_status(plan, target_mpm)
        refresh_pallet_stability_status(plan, pallet_dims, tolerance=constraint_config.center_of_mass_tolerance)
        candidate_plans.append(plan)

    gate = validate_plan_constraints(
        candidate_plans, pallet_dims, constraint_config=constraint_config
    )
    if not gate["is_valid"]:
        return diag

    type_plans[:] = candidate_plans

    diag["rescued"] = new_success - old_success
    diag["recipe_rebuild_success"] = 1
    return diag
