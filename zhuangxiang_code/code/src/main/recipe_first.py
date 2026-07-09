"""配方优先装箱编排。

对每个分组：
1. 配方配额规划（recipe_planner）→ 逐实例用 _initial_pack 实装，
   必须"整池装入 + 达标 + 整盘门禁"，装不出的实例箱回落剩余池；
2. 剩余箱再跑一遍 pack_group 兜底（含守恒兜底托盘）；
3. 守恒校验：配方方案的箱子 id 集合与数量必须与输入一致。

两种模式（safe_compare 参数）：

- 快速模式（默认）：直接采用"配方实例 + 兜底"方案，不再额外跑一遍
  全量基线（实测三组基线均被配方方案取代，纯属冗余耗时）。仅当
  规划不出实例、实装全失败或守恒失败时，回退跑基线 pack_group，
  行为与原先一致。
- 审计模式（safe_compare=True，CLI --safe）：保留双跑棘轮——基线与
  配方方案都跑，仅当配方方案达标严格更多且守恒时才采用。用于定期
  审计或首次处理新类型数据。

不修改任何装箱原语；两种模式下硬约束门禁完全一致。
"""

import time
from typing import Dict, List, Optional, Tuple

from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.layered_packer import try_layered_order
from src.rescue import IndexBuilder

from .recipe_planner import plan_recipe_pools


def _success_count(plans: List[Dict]) -> int:
    return sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS')


def _box_ids(items: List[Dict]) -> set:
    return {b.get('id') for b in items}


def _sum_mpm(items: List[Dict]) -> float:
    return sum(float(b.get('min_pack_multiple', 0) or 0) for b in items)


def _oriented_for_pallet(
    box: Dict, pallet_dims: Dict, target_mpm: Optional[float], tol: float = 2.0
) -> Dict:
    """为箱子选最优朝向（90° 旋转），返回副本；带几何门槛避免扰动难组。

    仅当旋转能让该箱型「单类整层」从不达标变达标（cap_fix < target ≤ cap_rot）
    时才换向——精准救回旋转敏感订单（固定朝向装不满），同时对「旋转也救不了
    的难组」保持原朝向，不打乱其已紧凑的布局（保填充率、不增盘数）。
    保留 id，不改原 box（不污染 GCP 路径或共享引用）。
    """
    L = float(box.get('length', 0) or 0)
    W = float(box.get('width', 0) or 0)
    H = float(box.get('height', 0) or 0)
    mpm = float(box.get('min_pack_multiple', 0) or 0)
    PL = float(pallet_dims.get('length', 0) or 0)
    PW = float(pallet_dims.get('width', 0) or 0)
    PH = float(pallet_dims.get('height', 0) or 0)
    if L <= 0 or W <= 0 or PL <= 0 or PW <= 0 or abs(L - W) < 1e-6:
        return box
    per_fix = int(PL // (L + tol)) * int(PW // (W + tol))
    per_rot = int(PL // (W + tol)) * int(PW // (L + tol))
    if per_rot <= per_fix:
        return box
    # 几何门槛：旋转须能让单类整层从不达标跨到达标，才值得换向。
    if target_mpm is not None and H > 0 and mpm > 0 and PH > 0:
        layers = int(PH // H)
        cap_fix = per_fix * layers * mpm
        cap_rot = per_rot * layers * mpm
        if not (cap_fix + 1e-9 < float(target_mpm) <= cap_rot + 1e-9):
            return box
    nb = dict(box)
    nb['length'], nb['width'] = W, L
    return nb


def _pack_instance(
    packer,
    pool: List[Dict],
    target_mpm: float,
    pallet_dims: Dict,
    instance_idx: int,
) -> Optional[List[Dict]]:
    """实装一个配方池：整池装入 + 达标 + 整盘门禁，失败返回 None。"""
    tried = []
    for counter in (instance_idx, 1):
        if counter in tried:
            continue
        tried.append(counter)
        diag = {
            'hard_recipe_attempts': 0,
            'hard_recipe_candidates': 0,
            'hard_recipe_selected': 0,
        }
        packed = packer._initial_pack(
            pool, target_mpm, pallet_dims, counter,
            fill_aware=False, hard_recipe_diag=diag,
        )
        if not packed:
            continue
        if {b.get('id') for b in packed} != _box_ids(pool):
            continue
        if _sum_mpm(packed) + 1e-9 < target_mpm:
            continue
        gate = validate_pallet_constraints(
            {'packed_items': packed}, pallet_dims,
            constraint_config=getattr(packer, '_cfg', None),
        )
        if gate['is_valid']:
            return packed
    return None


def _build_recipe_plan(
    packer,
    pallet_type: str,
    sales_order_no: str,
    boxes_in_group: List[Dict],
    target_mpm: float,
    pallet_dims: Dict,
    recipe_diag: Dict,
) -> Tuple[Optional[List[Dict]], Dict, Dict]:
    """实装全部配方实例 + 剩余箱兜底，返回 (方案, 兜底耗时, 兜底诊断)。

    守恒失败返回 (None, ..)，由调用方回退基线。

    计时口径：recipe_seconds 只覆盖"规划 + 实例实装"，兜底段耗时由
    left_rt 单独承载，避免 packing 桶重复计数。
    """
    t0 = time.time()
    pools, _meta = plan_recipe_pools(boxes_in_group, target_mpm, pallet_dims)
    recipe_diag['recipe_planned'] = len(pools)
    if not pools:
        recipe_diag['recipe_seconds'] += time.time() - t0
        return None, {}, {}

    t_recipe = time.time()
    instances: List[Dict] = []
    for idx, pool in enumerate(pools, 1):
        packed = _pack_instance(packer, pool, target_mpm, pallet_dims, idx)
        if packed is None:
            recipe_diag['recipe_pack_fail'] += 1
            continue
        total = _sum_mpm(packed)
        instances.append({
            'pallet_id': None,  # 合并后统一重编号
            'pallet_type': pallet_type,
            'sales_order_no': sales_order_no,
            'packed_items': packed,
            'mpm_total': total,
            'mpm_target': target_mpm,
            'mpm_gap': target_mpm - total,
            'mpm_status': 'SUCCESS',
            'stability_checks': {'status': 'SUCCESS'},
        })
        recipe_diag['recipe_packed'] += 1
    recipe_diag['recipe_seconds'] += time.time() - t0

    if not instances:
        return None, {}, {}

    used_ids = set()
    for sol in instances:
        used_ids |= _box_ids(sol['packed_items'])
    # 实装失败实例的箱子 id 不在 used_ids 内，天然回落到剩余池
    leftover = [b for b in boxes_in_group if b.get('id') not in used_ids]
    if leftover:
        left_plan, left_rt, left_diag = packer.pack_group(
            pallet_type, sales_order_no, leftover, target_mpm
        )
    else:
        left_plan = []
        left_rt = {'packing': 0.0, 'topup': 0.0, 'retry': 0.0}
        left_diag = {}

    recipe_plan = instances + left_plan
    for i, sol in enumerate(recipe_plan, 1):
        sol['pallet_id'] = '%s-%s-%d' % (pallet_type, sales_order_no, i)

    # 守恒校验：配方方案的箱子 id 集合与数量都必须与输入一致
    out_ids_list = [
        b.get('id') for sol in recipe_plan for b in sol['packed_items']
    ]
    conserved = (
        set(out_ids_list) == _box_ids(boxes_in_group)
        and len(out_ids_list) == len(boxes_in_group)
    )
    recipe_diag['recipe_conserved'] = conserved
    if not conserved:
        return None, {}, {}
    return recipe_plan, left_rt, left_diag


def pack_group_recipe_first(
    packer,
    pallet_type: str,
    sales_order_no: str,
    boxes_in_group: List[Dict],
    target_mpm: Optional[float],
    safe_compare: bool = False,
    allow_box_rotation: bool = True,
) -> Tuple[List[Dict], Dict, Dict]:
    """配方优先的分组装箱。返回与 packer.pack_group 相同的三元组。"""
    if target_mpm is None or not boxes_in_group:
        return packer.pack_group(
            pallet_type, sales_order_no, boxes_in_group, target_mpm
        )

    pallet_dims = boxes_in_group[0]['pallet_dims']

    # L1 朝向规整（带几何门槛）：仅对「旋转能让单类整层从不达标变达标」的
    # 箱型换向（救旋转敏感订单），对「旋转也救不了的难组」保持原朝向（保填充）。
    # 生成副本、保留 id，不污染 GCP（GCP 走 _run_group_gcp 不经过本函数）。
    if allow_box_rotation:
        boxes_in_group = [
            _oriented_for_pallet(b, pallet_dims, target_mpm)
            for b in boxes_in_group
        ]

    # 列式装箱优先（仅快速模式）：整单恰好一托盘且能列式装出达标方案时
    # 直接采用；任何不满足条件都返回 None 回落现有配方/基线流程——纯增量、
    # 零回归。审计模式（--safe）不介入，保持原配方/基线对比口径。
    if not safe_compare:
        t_layered = time.time()
        layered_plan = try_layered_order(
            packer, boxes_in_group, target_mpm, pallet_dims
        )
        if layered_plan is not None:
            for i, sol in enumerate(layered_plan, 1):
                sol['pallet_id'] = '%s-%s-%d' % (pallet_type, sales_order_no, i)
                sol['pallet_type'] = pallet_type
                sol['sales_order_no'] = sales_order_no
            print(
                '  - 列式装箱[fast]：整单单托盘达标 %d 盘，直接采用。'
                % len(layered_plan)
            )
            index_diag = IndexBuilder.build_index_diagnostics(
                boxes_in_group, target_mpm, pallet_dims
            )
            index_diag['layered_first'] = {'adopted': True}
            pack_rt = {
                'packing': time.time() - t_layered,
                'topup': 0.0,
                'retry': 0.0,
            }
            return layered_plan, pack_rt, index_diag

    recipe_diag = {
        'recipe_planned': 0,
        'recipe_packed': 0,
        'recipe_pack_fail': 0,
        'recipe_adopted': False,
        'recipe_mode': 'safe' if safe_compare else 'fast',
        'recipe_seconds': 0.0,
    }
    # recipe_seconds 由 _build_recipe_plan 内部记账（规划+实装），
    # 兜底段耗时在 left_rt 中单独承载，二者在 _assemble_recipe_result
    # 中合并进 packing 桶，互不重复。
    recipe_plan, left_rt, left_diag = _build_recipe_plan(
        packer, pallet_type, sales_order_no, boxes_in_group,
        target_mpm, pallet_dims, recipe_diag,
    )

    if safe_compare or recipe_plan is None:
        # 审计模式，或配方路径不可用（无实例/守恒失败）→ 跑基线
        base_plan, base_rt, base_diag = packer.pack_group(
            pallet_type, sales_order_no, boxes_in_group, target_mpm
        )
        base_diag['recipe_first'] = recipe_diag
        if recipe_plan is None:
            print(
                '  - 配方优先[%s]：规划 %d 实例，实装成功 %d，'
                '配方路径不可用 → 采用基线方案。'
                % (recipe_diag['recipe_mode'], recipe_diag['recipe_planned'],
                   recipe_diag['recipe_packed'])
            )
            return base_plan, base_rt, base_diag

        base_succ = _success_count(base_plan)
        rec_succ = _success_count(recipe_plan)
        adopted = rec_succ > base_succ
        recipe_diag['recipe_adopted'] = adopted
        print(
            '  - 配方优先[safe]：规划 %d 实例，实装成功 %d，失败回兜底 %d；'
            '达标 配方=%d vs 基线=%d → 采用%s。'
            % (recipe_diag['recipe_planned'], recipe_diag['recipe_packed'],
               recipe_diag['recipe_pack_fail'], rec_succ, base_succ,
               '配方方案' if adopted else '基线方案')
        )
        if not adopted:
            return base_plan, base_rt, base_diag
        return _assemble_recipe_result(
            recipe_plan, base_rt, base_diag, left_rt, left_diag, recipe_diag
        )

    # 快速模式：直接采用配方方案，不再跑冗余基线
    recipe_diag['recipe_adopted'] = True
    print(
        '  - 配方优先[fast]：规划 %d 实例，实装成功 %d，失败回兜底 %d；'
        '达标 %d，直接采用配方方案（基线未跑，--safe 可审计对比）。'
        % (recipe_diag['recipe_planned'], recipe_diag['recipe_packed'],
           recipe_diag['recipe_pack_fail'], _success_count(recipe_plan))
    )
    index_diag = IndexBuilder.build_index_diagnostics(
        boxes_in_group, target_mpm, pallet_dims
    )
    index_diag['index_target_unreachable'] = (
        _sum_mpm(boxes_in_group) + 1e-9 < float(target_mpm)
    )
    canonical = (index_diag.get('canonical_layer_best') or {}).get('best_mpm')
    index_diag['geometric_target_unreachable'] = (
        canonical is not None and float(canonical) + 1e-9 < float(target_mpm)
    )
    base_rt = {'packing': 0.0, 'topup': 0.0, 'retry': 0.0}
    return _assemble_recipe_result(
        recipe_plan, base_rt, index_diag, left_rt, left_diag, recipe_diag
    )


def _assemble_recipe_result(
    recipe_plan: List[Dict],
    base_rt: Dict,
    base_diag: Dict,
    left_rt: Dict,
    left_diag: Dict,
    recipe_diag: Dict,
) -> Tuple[List[Dict], Dict, Dict]:
    pack_rt = {
        'packing': (
            base_rt.get('packing', 0.0)
            + left_rt.get('packing', 0.0)
            + recipe_diag['recipe_seconds']
        ),
        'topup': base_rt.get('topup', 0.0) + left_rt.get('topup', 0.0),
        'retry': base_rt.get('retry', 0.0) + left_rt.get('retry', 0.0),
    }
    index_diag = dict(base_diag)
    index_diag['recipe_first'] = recipe_diag
    # 救援链的 tail-absorb 跳过判定应反映被采用方案（兜底段）的实际情况
    index_diag['main_tail_absorb'] = (left_diag or {}).get(
        'main_tail_absorb', {}
    ) or {}
    return recipe_plan, pack_rt, index_diag
