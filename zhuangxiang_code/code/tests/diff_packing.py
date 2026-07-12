"""差分两个装箱方案 JSON：逐托盘比较 packed_items 的 (id, position, dims)，
并核验守恒（不漏/不重/无空盘）与达标盘数变化。

用法:
    python code/tests/diff_packing.py <golden.json> <new.json> [--mode equivalent|guarded]
退出码 0 = 通过；1 = 不通过；2 = 参数错误。
"""
import sys
import json


def _load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _pallet_item_map(plan):
    """{pallet_id: {box_id: (x,y,z,L,W,H 四舍五入)}}"""
    out = {}
    for pal in plan.get('pallets', []):
        items = {}
        for it in pal.get('packed_items', []):
            pos = it.get('position') or {}
            items[str(it.get('id'))] = (
                round(float(pos.get('x', 0) or 0), 3),
                round(float(pos.get('y', 0) or 0), 3),
                round(float(pos.get('z', 0) or 0), 3),
                round(float(it.get('length', 0) or 0), 3),
                round(float(it.get('width', 0) or 0), 3),
                round(float(it.get('height', 0) or 0), 3),
            )
        out[pal.get('pallet_id')] = items
    return out


def _all_box_ids(plan):
    ids = []
    for pal in plan.get('pallets', []):
        for it in pal.get('packed_items', []):
            ids.append(str(it.get('id')))
    return ids


def _success_count(plan):
    return sum(
        1 for pal in plan.get('pallets', [])
        if pal.get('mpm_status') == 'SUCCESS'
    )


def diff(golden_path, new_path):
    g = _load(golden_path)
    n = _load(new_path)

    g_ids = _all_box_ids(g)
    n_ids = _all_box_ids(n)
    g_set, n_set = set(g_ids), set(n_ids)

    missing = g_set - n_set
    extra = n_set - g_set
    dups = len(n_ids) - len(n_set)
    empty = [
        pal.get('pallet_id') for pal in n.get('pallets', [])
        if not pal.get('packed_items')
    ]

    g_succ, n_succ = _success_count(g), _success_count(n)
    gp, npm = _pallet_item_map(g), _pallet_item_map(n)
    identical = (gp == npm)

    print('=== 差分报告 ===')
    print('golden:', golden_path)
    print('new   :', new_path)
    print('托盘数 golden=%d new=%d' % (len(gp), len(npm)))
    print('达标盘 golden=%d new=%d (delta=%+d)' % (g_succ, n_succ, n_succ - g_succ))
    print('守恒: 漏箱=%d 多箱=%d 重复箱=%d 空盘=%d'
          % (len(missing), len(extra), dups, len(empty)))
    print('逐托盘装箱完全一致:', identical)
    if not identical:
        diff_pallets = [
            pid for pid in (set(gp) | set(npm))
            if gp.get(pid) != npm.get(pid)
        ]
        print('差异托盘数=%d 例如=%s' % (len(diff_pallets), diff_pallets[:5]))

    conserved = (not missing) and (not extra) and (dups == 0) and (not empty)
    return {
        'identical': identical,
        'conserved': conserved,
        'success_delta': n_succ - g_succ,
    }


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('用法: python code/tests/diff_packing.py <golden.json> <new.json> '
              '[--mode equivalent|guarded]')
        sys.exit(2)
    mode = 'equivalent'
    if '--mode' in sys.argv:
        idx = sys.argv.index('--mode') + 1
        if idx >= len(sys.argv):
            print('错误：--mode 缺少取值（equivalent|guarded）')
            sys.exit(2)
        mode = sys.argv[idx]
    if mode not in ('equivalent', 'guarded'):
        print('错误：--mode 取值须为 equivalent 或 guarded，收到 %r' % mode)
        sys.exit(2)
    res = diff(sys.argv[1], sys.argv[2])
    if mode == 'equivalent':
        ok = res['identical'] and res['conserved']
        print('[%s] 等价模式: 逐托盘一致 + 守恒' % ('PASS' if ok else 'FAIL'))
    else:
        ok = res['conserved'] and res['success_delta'] >= 0
        print('[%s] 守护模式: 守恒 + 达标盘不减(delta=%+d)'
              % ('PASS' if ok else 'FAIL', res['success_delta']))
    sys.exit(0 if ok else 1)
