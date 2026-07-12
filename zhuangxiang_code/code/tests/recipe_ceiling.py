"""Phase A：配方配额规划上限测算（已验证可实现口径）。

流程：每组 规划(plan_recipe_pools) → 对计划中每种配方用真实
_initial_pack 实装一次并过整盘门禁 → 装不出的配方加入黑名单重规划
（最多 5 轮）→ 输出三组"已验证可实现的达标盘数" vs 当前实际。

用法: python code/tests/recipe_ceiling.py
"""
import sys
import json
import time
from pathlib import Path
from collections import defaultdict

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.data import load_boxes
from src.config import SMALL_BOX_SOURCE_FILE, PALLET_INDEX_TARGETS
from src.geometry.constraint_validator import validate_pallet_constraints
from src.main.recipe_planner import plan_recipe_pools, describe_recipe
from src.utils.helpers import sum_item_mpm
from run_packing import build_workflow


def current_success_by_group(plan_path):
    try:
        with open(plan_path, encoding='utf-8') as f:
            plan = json.load(f)
    except OSError:
        return {}
    out = defaultdict(int)
    for pal in plan.get('pallets', []):
        if pal.get('mpm_status') == 'SUCCESS':
            out[(pal.get('pallet_type'), pal.get('sales_order_no'))] += 1
    return dict(out)


def validate_recipe(packer, pool, target, pallet_dims):
    """用真实装箱器实装一个配方池：必须整池装入、达标、过整盘门禁。"""
    diag = {
        'hard_recipe_attempts': 0,
        'hard_recipe_candidates': 0,
        'hard_recipe_selected': 0,
    }
    packed = packer._initial_pack(pool, target, pallet_dims, 1,
                                  fill_aware=False, hard_recipe_diag=diag)
    if not packed:
        return False, 'pack_empty'
    if {b['id'] for b in packed} != {b['id'] for b in pool}:
        return False, 'partial_pool(%d/%d)' % (len(packed), len(pool))
    if sum_item_mpm(packed) + 1e-9 < target:
        return False, 'below_target'
    gate = validate_pallet_constraints({'packed_items': packed}, pallet_dims)
    if not gate['is_valid']:
        return False, 'gate:%s' % gate['violations'][:1]
    return True, 'ok'


def main():
    t0 = time.time()
    boxes = load_boxes(str(SMALL_BOX_SOURCE_FILE))
    groups = defaultdict(list)
    for b in boxes:
        groups[(b['pallet_type'], b['sales_order_no'])].append(b)

    cur = current_success_by_group('output/NEW_full_final.json')
    wf = build_workflow()
    packer = wf.packer

    grand_planned = 0
    grand_cur = 0
    for key in sorted(groups):
        ptype, sorder = key
        gboxes = groups[key]
        target = PALLET_INDEX_TARGETS.get(ptype)
        pallet_dims = gboxes[0]['pallet_dims']
        banned = set()
        pools, meta = [], {}
        for round_idx in range(5):
            pools, meta = plan_recipe_pools(
                gboxes, target, pallet_dims, banned_signatures=banned
            )
            # 对计划中每种"独特配方"实装验证一次
            distinct = {}
            for pool, rec in zip(pools, meta['pool_recipes']):
                distinct.setdefault((rec['layers'], rec['band']), (pool, rec))
            bad = []
            for sig, (pool, rec) in distinct.items():
                ok, reason = validate_recipe(packer, pool, target, pallet_dims)
                if not ok:
                    bad.append((sig, reason, rec))
            if not bad:
                break
            for sig, reason, rec in bad:
                banned.add(sig)
            print('  [round %d] 踢出 %d 个装不出的配方: %s'
                  % (round_idx, len(bad),
                     '; '.join('%s(%s)' % (describe_recipe(r, meta['types']), why)
                               for _, why, r in bad[:3])))
        cur_succ = cur.get(key, 0)
        grand_planned += len(pools)
        grand_cur += cur_succ
        print()
        print('== 组 %s %s: 当前达标=%d → 配方规划(已验证)=%d  (%+d)'
              % (ptype, sorder, cur_succ, len(pools), len(pools) - cur_succ))
        recipe_count = defaultdict(int)
        for rec in meta['pool_recipes']:
            recipe_count[(rec['layers'], rec['band'])] += 1
        for sig, n in sorted(recipe_count.items(), key=lambda kv: -kv[1]):
            rec = next(r for r in meta['pool_recipes']
                       if (r['layers'], r['band']) == sig)
            print('   x%-3d %s' % (n, describe_recipe(rec, meta['types'])))
        used = {b['id'] for pool in pools for b in pool}
        left_mpm = sum_item_mpm([b for b in gboxes if b['id'] not in used])
        print('   剩余箱指数=%.0f（留给兜底装箱继续消化）' % left_mpm)

    print()
    print('=' * 64)
    print('汇总: 当前达标=%d → 配方规划已验证可实现=%d  (%+d)'
          % (grand_cur, grand_planned, grand_planned - grand_cur))
    print('耗时 %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
