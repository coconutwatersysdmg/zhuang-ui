"""成功盘↔失败盘指数互换救援（全局通用）。

背景：贪心装箱可能把高指数箱"浪费"在本来就能达标的托盘上（表现为
填充率不高但指数超过目标的成功盘），同时失败盘差一点指数。把成功盘的
少量高指数箱与失败盘的低指数箱互换，可以在成功盘保持达标的前提下，
把失败盘提到目标指数。

数学事实（决定本救援的适用范围）：两盘互换时指数守恒，成功盘保持
≥ target 的前提下，能净注入失败盘的指数不超过该成功盘的富余
σ = mpm - target。因此本救援只在组内存在 σ>0 的成功盘时生效——
配方/beam "装满优先"路径会产出超标盘（σ>0）；GCP 按 192 精准构建的
成功盘 σ=0，天然不参与、也绝不会被动过。诊断字段 swap_surplus_total
直接暴露组内可用富余，便于判断"为什么没换"。

流程（每次互换，外科手术式、不整盘重装）：
1. 失败盘按缺口升序、成功盘按富余降序配对；
2. 选让渡集 A ⊆ 成功盘（|A|≤2，高指数箱）与回补集 B ⊆ 失败盘
   （|B|≤4，低指数箱），使净转移 T = mpm(A) - mpm(B) ∈ [缺口, σ]，
   并尽量贴近缺口（节省富余，留给后续失败盘）；A、B 只能取
   "上方无箱"的箱子（摘除不破坏他箱支撑）；
3. 两盘各自在**既有布局**上摘除让出的箱、用增量候选生成器
   （与 hole_fill 救援同源，含吸盘位姿）逐箱插入换入的箱；
4. 双盘整盘门禁（带 target：达标盘免 gap，物理约束恒查）+ 重心校验
   + 两盘箱子 id 并集守恒；
5. 全部通过且双双达标才提交（原地替换 packed_items），否则原样不动。
"""

import random
import time
from copy import deepcopy
from typing import Callable, Dict, List, Optional, Tuple

from src.geometry.constraint_validator import validate_pallet_constraints
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import has_box_above

#: 单组互换的墙钟预算（秒）与最大成功次数（防超大组耗时失控）
SWAP_TIME_BUDGET_S = 30.0
MAX_SWAPS_PER_GROUP = 8
#: 让渡/回补集合的规模上限（组合搜索有界）
MAX_GIVE_BOXES = 2
MAX_TAKE_BOXES = 4
#: 重装后的体积安全系数（超过则不必尝试重装）
VOLUME_SAFETY = 0.98


def swap_success_failed(
    type_plans: List[Dict],
    target_mpm: Optional[float],
    pallet_dims: Dict[str, float],
    packer_cls,
    validate_com: Optional[Callable],
    constraint_config,
    time_budget_s: float = SWAP_TIME_BUDGET_S,
    max_swaps: int = MAX_SWAPS_PER_GROUP,
) -> Dict:
    """在一个分组内执行成功盘↔失败盘指数互换。

    Args:
        type_plans: 分组全部托盘方案（原地修改，仅提交成功的互换）。
        target_mpm: 目标指数。
        pallet_dims: 托盘尺寸。
        packer_cls: 真实装箱器类（BeamSearchPacker）。
        validate_com: 重心校验函数（与其它救援器同源注入）。
        constraint_config: 约束统一配置。

    Returns:
        诊断 dict：swap_tried / swap_accepted / swap_surplus_total /
        swap_reason。swap_accepted 即新增达标盘数。
    """
    diag = {
        "swap_tried": 0,
        "swap_accepted": 0,
        "swap_surplus_total": 0.0,
        "swap_reason": "",
    }
    if target_mpm is None or packer_cls is None or not pallet_dims:
        diag["swap_reason"] = "no_target_or_packer"
        return diag
    target = float(target_mpm)
    pallet_volume = (
        float(pallet_dims.get('length', 0) or 0)
        * float(pallet_dims.get('width', 0) or 0)
        * float(pallet_dims.get('height', 0) or 0)
    )
    if pallet_volume <= 0:
        diag["swap_reason"] = "no_pallet_dims"
        return diag

    for plan in type_plans:
        PalletEvaluator.calc_pallet_status(plan)
    donors = [
        p for p in type_plans
        if p.get('mpm_status') == 'SUCCESS'
        and _sum_mpm(p.get('packed_items', [])) - target > 1e-9
    ]
    receivers = [
        p for p in type_plans
        if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
    ]
    diag["swap_surplus_total"] = round(
        sum(_sum_mpm(p['packed_items']) - target for p in donors), 3
    )
    if not receivers:
        diag["swap_reason"] = "no_failed_pallet"
        return diag
    if not donors:
        diag["swap_reason"] = "no_surplus_donor"
        return diag

    deadline = time.time() + time_budget_s
    receivers.sort(key=lambda p: (target - _sum_mpm(p['packed_items']),
                                  str(p.get('pallet_id'))))
    seed = 90017
    for recv in receivers:
        if diag["swap_accepted"] >= max_swaps or time.time() > deadline:
            break
        gap = target - _sum_mpm(recv['packed_items'])
        if gap <= 1e-9:
            continue
        # 富余大的 donor 优先（更可能覆盖缺口；用后富余实时更新）
        donors.sort(
            key=lambda p: -( _sum_mpm(p['packed_items']) - target )
        )
        committed = False
        for donor in donors:
            surplus = _sum_mpm(donor['packed_items']) - target
            if surplus + 1e-9 < gap:
                break  # 降序排列，后面的更小
            choices = _find_swap_candidates(
                donor['packed_items'], recv['packed_items'],
                gap, surplus, pallet_volume,
            )
            for give, take in choices:
                if time.time() > deadline:
                    break
                diag["swap_tried"] += 1
                if _try_commit_swap(
                    donor, recv, give, take, target, pallet_dims,
                    packer_cls, validate_com, constraint_config, seed,
                ):
                    diag["swap_accepted"] += 1
                    committed = True
                seed += 31
                if committed:
                    break
            if committed or time.time() > deadline:
                break
    if diag["swap_accepted"] > 0:
        diag["swap_reason"] = "ok"
    elif not diag["swap_reason"]:
        diag["swap_reason"] = (
            "no_feasible_swap" if diag["swap_tried"] else "surplus_insufficient"
        )
    return diag


# ----------------------------------------------------------------------
# 集合选择
# ----------------------------------------------------------------------

def _find_swap_candidates(
    donor_items: List[Dict],
    recv_items: List[Dict],
    gap: float,
    surplus: float,
    pallet_volume: float,
    max_candidates: int = 6,
) -> List[Tuple[List[Dict], List[Dict]]]:
    """枚举让渡集 A / 回补集 B 候选：T = mpm(A) - mpm(B) ∈ [gap, surplus]。

    只考虑"上方无箱"的箱子（摘除不破坏他箱支撑，外科手术可行）。
    排序偏好：接收盘净腾空间 vol(B)-vol(A) 大者优先（失败盘往往并不空，
    换出低指数大箱、换入高指数小箱才插得进——这正是"指数密度再分配"），
    其次净转移 T 小（省富余）。返回前 max_candidates 个，插入失败时调用方
    可尝试下一个。
    """
    eps = 1e-9
    donor_free = [
        b for b in donor_items if not has_box_above(b, donor_items)
    ]
    recv_free = [
        b for b in recv_items if not has_box_above(b, recv_items)
    ]
    donor_sorted = sorted(
        donor_free,
        key=lambda b: (_mpm(b), str(b.get('id'))),
    )
    give_candidates: List[List[Dict]] = [
        [b] for b in donor_sorted if _mpm(b) + eps >= gap
    ]
    # 高指数双箱组合（单箱盖不住缺口时的补充，也给体积平衡更多选择）
    top = sorted(donor_sorted, key=_mpm, reverse=True)[:6]
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            if _mpm(top[i]) + _mpm(top[j]) + eps >= gap:
                give_candidates.append([top[i], top[j]])

    scored: List[Tuple[float, float, List[Dict], List[Dict]]] = []
    seen = set()
    for give in give_candidates[:16]:
        give_mpm = sum(_mpm(b) for b in give)
        lo = max(0.0, give_mpm - surplus)
        hi = give_mpm - gap
        if hi < -eps:
            continue
        take = _pick_take_subset(recv_free, lo, hi)
        if take is None:
            continue
        # 体积粗筛：交换后两盘都不能超过托盘容积
        recv_vol = (
            _items_volume(recv_items) - _items_volume(take)
            + _items_volume(give)
        )
        donor_vol = (
            _items_volume(donor_items) - _items_volume(give)
            + _items_volume(take)
        )
        if recv_vol > pallet_volume * VOLUME_SAFETY + 1e-6:
            continue
        if donor_vol > pallet_volume * VOLUME_SAFETY + 1e-6:
            continue
        t_net = give_mpm - sum(_mpm(b) for b in take)
        freed_vol = _items_volume(take) - _items_volume(give)
        key = tuple(sorted(str(b.get('id')) for b in give))
        if key in seen:
            continue
        seen.add(key)
        scored.append((-freed_vol, t_net, give, take))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [(give, take) for _, _, give, take in scored[:max_candidates]]


def _pick_take_subset(
    items: List[Dict], lo: float, hi: float
) -> Optional[List[Dict]]:
    """从失败盘挑回补集 B：mpm(B) ∈ [lo, hi]，尽量大（净转移贴近缺口）。

    有界深搜（|B| ≤ MAX_TAKE_BOXES）；lo≤0 时允许 B 为空。
    """
    eps = 1e-9
    if hi < -eps:
        return None
    if lo <= eps:
        best_empty: Optional[List[Dict]] = []
    else:
        best_empty = None
    # mpm 降序、同 mpm 大体积优先（换出低指数大箱 → 接收盘净腾空间）
    pool = sorted(items, key=lambda b: (-_mpm(b), -_box_volume(b)))
    pool = [b for b in pool if _mpm(b) <= hi + eps]
    best: Optional[Tuple[float, List[Dict]]] = None

    def dfs(start: int, chosen: List[Dict], total: float) -> None:
        nonlocal best
        if total > hi + eps:
            return
        if total + eps >= lo and chosen:
            if best is None or total > best[0] + eps:
                best = (total, list(chosen))
        if len(chosen) >= MAX_TAKE_BOXES or start >= len(pool):
            return
        if best is not None and abs(best[0] - hi) <= eps:
            return  # 已贴上界
        for idx in range(start, len(pool)):
            chosen.append(pool[idx])
            dfs(idx + 1, chosen, total + _mpm(pool[idx]))
            chosen.pop()

    dfs(0, [], 0.0)
    if best is not None:
        return best[1]
    return best_empty


# ----------------------------------------------------------------------
# 重装与提交
# ----------------------------------------------------------------------

def _try_commit_swap(
    donor: Dict,
    recv: Dict,
    give: List[Dict],
    take: List[Dict],
    target: float,
    pallet_dims: Dict[str, float],
    packer_cls,
    validate_com: Optional[Callable],
    constraint_config,
    seed: int,
) -> bool:
    """外科手术式互换 + 门禁 + 守恒校验；全部通过才原地提交。

    两盘都在既有布局上操作：摘除让出的箱（选箱时已保证上方无箱），
    再用增量候选生成器逐箱插入换入的箱——不做整盘重装，成功率高、
    且不动盘上其余箱子的摆放。
    """
    give_ids = {b.get('id') for b in give}
    take_ids = {b.get('id') for b in take}

    packed_donor = _surgical_exchange(
        donor['packed_items'], give_ids, take,
        pallet_dims, packer_cls, constraint_config, seed,
    )
    if packed_donor is None:
        return False
    packed_recv = _surgical_exchange(
        recv['packed_items'], take_ids, give,
        pallet_dims, packer_cls, constraint_config, seed + 7,
    )
    if packed_recv is None:
        return False

    # 指数不变量（按构造成立，此处防御性复核）
    if _sum_mpm(packed_donor) + 1e-9 < target:
        return False
    if _sum_mpm(packed_recv) + 1e-9 < target:
        return False
    # 两盘箱子 id 并集守恒
    before = sorted(
        [str(i.get('id')) for i in donor['packed_items']]
        + [str(i.get('id')) for i in recv['packed_items']]
    )
    after = sorted(
        [str(i.get('id')) for i in packed_donor]
        + [str(i.get('id')) for i in packed_recv]
    )
    if before != after:
        return False

    for packed in (packed_donor, packed_recv):
        gate = validate_pallet_constraints(
            {'packed_items': packed}, pallet_dims,
            constraint_config=constraint_config, target_mpm=target,
        )
        if not gate['is_valid']:
            return False
    if validate_com is not None:
        for packed in (packed_donor, packed_recv):
            com = validate_com({'packed_items': packed}, pallet_dims)
            if not com.get('is_stable', False):
                return False

    # 提交：原地替换（保留 pallet_id 等元信息）
    for plan, packed in ((donor, packed_donor), (recv, packed_recv)):
        plan['packed_items'] = packed
        plan['rescue_swapped'] = True
        PalletEvaluator.calc_pallet_status(plan)
        plan['stability_checks'] = {'status': 'SUCCESS'}
    return True


def _surgical_exchange(
    items: List[Dict],
    remove_ids,
    insert_boxes: List[Dict],
    pallet_dims: Dict[str, float],
    packer_cls,
    constraint_config,
    seed: int,
) -> Optional[List[Dict]]:
    """在既有布局上摘除 remove_ids、逐箱增量插入 insert_boxes。

    插入用与 hole_fill 救援同源的候选生成器（含吸盘位姿与全部放置约束）；
    任何一箱插不进即失败返回 None。
    """
    placed = [deepcopy(i) for i in items if i.get('id') not in remove_ids]
    if not insert_boxes:
        return placed
    rng = random.Random(seed)
    packer = packer_cls(
        pallet_dims,
        support_ratio_threshold=constraint_config.support_ratio_threshold,
        size_tolerance=0.0,
        z_tolerance=0.0,
        max_candidate_points=240,
        max_points_per_layer=80,
        constraint_config=constraint_config,
    )
    for box in sorted(insert_boxes, key=_box_volume, reverse=True):
        candidates = packer._generate_feasible_candidates(
            dict(box), placed, rng
        )
        if not candidates:
            return None
        best = sorted(candidates, key=lambda c: c['score'])[0]
        placed.append(best['box'])
    return placed


def _sum_mpm(items: List[Dict]) -> float:
    return sum(float(i.get('min_pack_multiple', 0) or 0) for i in items)


def _mpm(box: Dict) -> float:
    return float(box.get('min_pack_multiple', 0) or 0)


def _box_volume(box: Dict) -> float:
    return (
        float(box.get('length', 0) or 0)
        * float(box.get('width', 0) or 0)
        * float(box.get('height', 0) or 0)
    )


def _items_volume(items: List[Dict]) -> float:
    return sum(
        float(i.get('length', 0) or 0)
        * float(i.get('width', 0) or 0)
        * float(i.get('height', 0) or 0)
        for i in items
    )
