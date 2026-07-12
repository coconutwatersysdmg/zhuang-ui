"""成品方案约束 + 规整度独立校验。

用代码库里的真实约束函数，对一份装箱方案 JSON 的每个托盘逐项核验：
- 整盘硬约束 validate_pallet_constraints（越界/间隙/支撑/重叠/吸盘字段/重心）；
- 小箱在下 passes_small_box_not_on_larger_constraint（逐箱：小箱正下方不得有更大箱）；
- 同尺寸重箱在下 passes_same_size_heavier_below_constraint；
并统计层级规整度（自底向上的层数、是否锚定角点、整层格栅）。

用法: python code/tests/verify_solution.py [plan.json]   (默认 output/NEW_full_s2.json)
"""
import sys
import json
from collections import defaultdict
from pathlib import Path

# Windows GBK 控制台兜底：避免打印 ✅/❌ 等字符时 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.geometry.constraint_validator import validate_pallet_constraints
from src.utils.helpers import (
    passes_small_box_not_on_larger_constraint,
)
from src.packing.stacking_policy import passes_same_size_heavier_below_constraint


def _dims(box):
    return {
        'length': float(box.get('length', 0) or 0),
        'width': float(box.get('width', 0) or 0),
        'height': float(box.get('height', 0) or 0),
    }


def check_constraints(pallets):
    violations = defaultdict(list)
    small_box_total = 0
    for pal in pallets:
        items = pal.get('packed_items', []) or []
        if not items:
            continue
        pallet_dims = items[0].get('pallet_dims') or {}
        pid = pal.get('pallet_id')

        # 1. 整盘硬约束（与生产门禁同一函数）
        gate = validate_pallet_constraints({'packed_items': items}, pallet_dims)
        if not gate['is_valid']:
            for v in gate['violations']:
                violations[v['type']].append((pid, v.get('box_id')))

        # 2/3. 逐箱：小箱在下 + 同尺寸重箱在下（放置时约束的静态等价核验）
        for i, box in enumerate(items):
            pos = box.get('position')
            if not pos:
                continue
            others = items[:i] + items[i + 1:]
            dims = _dims(box)
            if box.get('is_small_box'):
                small_box_total += 1
            if not passes_small_box_not_on_larger_constraint(box, pos, dims, others):
                violations['small_box_on_larger'].append((pid, box.get('id')))
            if not passes_same_size_heavier_below_constraint(box, pos, dims, others):
                violations['same_size_heavier_below'].append((pid, box.get('id')))
    return violations, small_box_total


def analyze_regularity(pallets):
    """统计 SUCCESS 托盘的层级规整度。"""
    success = [p for p in pallets if p.get('mpm_status') == 'SUCCESS']
    layer_hist = defaultdict(int)
    corner_anchored = 0
    floor_started = 0
    grid_pallets = 0
    for pal in success:
        items = pal.get('packed_items', []) or []
        if not items:
            continue
        zs = sorted({round(float(b['position']['z']), 1) for b in items})
        layer_hist[len(zs)] += 1
        min_x = min(float(b['position']['x']) for b in items)
        min_y = min(float(b['position']['y']) for b in items)
        if min_x < 1e-6 and min_y < 1e-6:
            corner_anchored += 1
        if zs and zs[0] < 1e-6:
            floor_started += 1
        # 整层格栅判定：每个 z 层内，箱子 x/y 是否都落在"该层最小箱长/宽"的整数倍网格上
        is_grid = True
        for z in zs:
            layer = [b for b in items if abs(float(b['position']['z']) - z) < 0.5]
            if not layer:
                continue
            base_l = min(float(b['length']) for b in layer)
            base_w = min(float(b['width']) for b in layer)
            for b in layer:
                rx = float(b['position']['x']) % base_l
                ry = float(b['position']['y']) % base_w
                if min(rx, base_l - rx) > 2.5 or min(ry, base_w - ry) > 2.5:
                    is_grid = False
                    break
            if not is_grid:
                break
        if is_grid:
            grid_pallets += 1
    return {
        'success_pallets': len(success),
        'layer_hist': dict(sorted(layer_hist.items())),
        'corner_anchored': corner_anchored,
        'floor_started': floor_started,
        'grid_pallets': grid_pallets,
    }


def print_sample_layout(pallets, n=2):
    success = [p for p in pallets if p.get('mpm_status') == 'SUCCESS']
    for pal in success[:n]:
        items = sorted(
            pal.get('packed_items', []),
            key=lambda b: (b['position']['z'], b['position']['y'], b['position']['x']),
        )
        print('--- 样例托盘 %s (%d 箱, mpm=%g) ---'
              % (pal.get('pallet_id'), len(items), pal.get('mpm_total')))
        for b in items:
            p = b['position']
            print('    z=%-5g (x=%-5g y=%-5g) %gx%gx%g type=%s%s'
                  % (p['z'], p['x'], p['y'], b['length'], b['width'], b['height'],
                     b.get('type'), ' [小箱]' if b.get('is_small_box') else ''))


def main(path):
    with open(path, encoding='utf-8') as f:
        plan = json.load(f)
    pallets = plan.get('pallets', [])
    print('=' * 64)
    print('校验方案:', path, '  托盘数:', len(pallets))

    violations, small_box_total = check_constraints(pallets)
    print('\n[硬约束 + 堆叠约束]  共扫描小箱 %d 个' % small_box_total)
    if not violations:
        print('  ✅ 全部托盘通过：越界/间隙/支撑/重叠/吸盘/重心/小箱在下(小箱不压大箱)/同尺寸重箱在下，0 违例。')
    else:
        for vtype, lst in violations.items():
            print('  ❌ %s: %d 例，例如 %s' % (vtype, len(lst), lst[:3]))

    reg = analyze_regularity(pallets)
    print('\n[层级规整度]  SUCCESS 托盘 %d 个' % reg['success_pallets'])
    print('  自底起装(最低层 z=0): %d/%d' % (reg['floor_started'], reg['success_pallets']))
    print('  锚定角点(min x=0 且 min y=0): %d/%d' % (reg['corner_anchored'], reg['success_pallets']))
    print('  整层格栅(每层箱子落在整数倍网格): %d/%d' % (reg['grid_pallets'], reg['success_pallets']))
    print('  层数分布(层数:托盘数): %s' % reg['layer_hist'])

    print()
    print_sample_layout(pallets, n=2)


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'output/NEW_full_s2.json'
    main(path)
