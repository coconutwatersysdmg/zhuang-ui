"""扫真实订单数据，量化「异质柱指数」盲区的占比。

精确触发器（来自 balanced_mpm 实验）：同一 (托盘类型, 销售订单号) 分组内，
**同一底面**(sorted round(L,W)) 出现 ≥2 种不同的「满柱指数」col_idx =
floor(palletH / round(boxH)) × mpm。满柱指数齐整 → 柱级 ILP 能均摊（如
mixed_height 同底面 240/480 但满柱都=6，GCP 4/4 无碍）；满柱指数异质 →
凑柱 FFD 凑出指数参差的柱 → ILP 难把指数均摊到达标（GCP 1/4）。

输出：每数据集的异质底面订单占比、箱数/指数占比，及异质程度分布。
用法：cd code && python -m tests.probe.scan_real_data
"""

import io
import sys
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_CODE_DIR = Path(__file__).resolve().parent.parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config.constants import DATA_DIR  # noqa: E402
from src.data import load_boxes  # noqa: E402

# 待扫真实数据集（相对 DATA_DIR）。不存在的自动跳过。
DATASETS = [
    '668箱子数据集.xlsx',
    '668箱子数据集2.xlsx',
    'selected_5000_boxes_9_1_concentrated_3orders.xlsx',
    '多条件筛选随机挑选 2000 个箱子最终结果(单托盘).xlsx',
    '多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx',
    '5000箱子数据集/5000箱子数据（9：1）/selected_5000_boxes_9_1_with_total_weight.xlsx',
    '5000箱子数据集/5000箱子数据（7：3）/selected_5000_boxes_7_3_with_total_weight.xlsx',
    '5000箱子数据集/5000箱子数据（10：0）/selected_5000_boxes_10_0_with_total_weight.xlsx',
    '5000箱子数据集/5000箱子数据（0：10）/selected_5000_boxes_0_10_with_total_weight.xlsx',
]


def _fp(b: Dict) -> Tuple[int, int]:
    return tuple(sorted((int(round(float(b.get('length', 0) or 0))),
                         int(round(float(b.get('width', 0) or 0))))))


def _col_idx(b: Dict) -> float:
    """该箱型同质满柱指数 = floor(palletH / boxH) × mpm。"""
    h = int(round(float(b.get('height', 0) or 0)))
    ph = float((b.get('pallet_dims') or {}).get('height', 720) or 720)
    mpm = float(b.get('min_pack_multiple', 0) or 0)
    per_col = int(ph // h) if h > 0 else 0
    return per_col * mpm


def analyze_group(boxes: List[Dict]) -> Dict:
    """分析一个 (托盘类型, 订单) 分组的底面级异质性。"""
    by_fp: Dict[Tuple[int, int], List[Dict]] = defaultdict(list)
    for b in boxes:
        by_fp[_fp(b)].append(b)
    n_fp = len(by_fp)
    het_fp = 0           # 满柱指数异质的底面数（含高度驱动）
    het_boxes = 0        # 异质底面涉及的箱数
    het_index = 0.0      # 异质底面涉及的总指数
    same_size_diff_mpm = 0  # ★精确触发器：同尺寸(底面+高)不同 mpm 的底面数
    max_distinct_colidx = 1
    max_mpm_per_size = 1    # 任一(底面,高)桶内最多几种 mpm（>1=精确触发器命中）
    for fp, grp in by_fp.items():
        colidxs = {round(_col_idx(b), 3) for b in grp}
        if len(colidxs) >= 2:
            het_fp += 1
            het_boxes += len(grp)
            het_index += sum(float(b.get('min_pack_multiple', 0) or 0) for b in grp)
            max_distinct_colidx = max(max_distinct_colidx, len(colidxs))
        # ★精确触发器：同尺寸不同 mpm（balanced_mpm 盲区的真实形态）
        by_size: Dict[Tuple, set] = defaultdict(set)
        for b in grp:
            sz = (fp, int(round(float(b.get('height', 0) or 0))))
            by_size[sz].add(round(float(b.get('min_pack_multiple', 0) or 0), 3))
        if any(len(v) >= 2 for v in by_size.values()):
            same_size_diff_mpm += 1
        max_mpm_per_size = max([max_mpm_per_size] + [len(v) for v in by_size.values()])
    return {
        'n_fp': n_fp, 'het_fp': het_fp, 'het_boxes': het_boxes,
        'het_index': het_index, 'same_size_diff_mpm': same_size_diff_mpm,
        'max_distinct_colidx': max_distinct_colidx,
        'max_mpm_per_size': max_mpm_per_size,
        'n_boxes': len(boxes),
        'total_index': sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes),
    }


def scan_dataset(path: Path) -> Optional[Dict]:
    try:
        with redirect_stdout(io.StringIO()):
            boxes = load_boxes(str(path))
    except Exception as exc:  # noqa: BLE001 不同 sheet 名/结构的数据集跳过
        return {'name': path.name, 'error': f'{type(exc).__name__}: {exc}'}
    if not boxes:
        return None
    groups: Dict[Tuple, List[Dict]] = defaultdict(list)
    for b in boxes:
        groups[(b.get('pallet_type'), b.get('sales_order_no'))].append(b)
    n_groups = len(groups)
    het_groups = 0
    sized_groups = 0
    tot_boxes = len(boxes)
    tot_index = sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes)
    het_boxes = 0
    het_index = 0.0
    max_mpm_per_size = 1
    worst = []
    for key, gb in groups.items():
        a = analyze_group(gb)
        max_mpm_per_size = max(max_mpm_per_size, a['max_mpm_per_size'])
        if a['het_fp'] > 0:
            het_groups += 1
            het_boxes += a['het_boxes']
            het_index += a['het_index']
            worst.append((key, a))
        if a['same_size_diff_mpm'] > 0:
            sized_groups += 1
    worst.sort(key=lambda x: -x[1]['het_index'])
    return {
        'name': path.name, 'n_boxes': tot_boxes, 'n_groups': n_groups,
        'het_groups': het_groups, 'sized_groups': sized_groups,
        'max_mpm_per_size': max_mpm_per_size,
        'het_box_share': het_boxes / tot_boxes if tot_boxes else 0,
        'het_index_share': het_index / tot_index if tot_index else 0,
        'worst': worst[:3],
    }


def main() -> None:
    print('扫真实订单：异质柱盲区触发器占比\n' + '=' * 78)
    print('★精确触发器 = 同尺寸(底面+高)不同 mpm（balanced_mpm 盲区的真实形态）')
    print('  对照列 = 同底面满柱指数异质（含高度驱动；668=11/11 证明已被 GCP 处理）\n')
    head = (f"{'数据集':<44}{'箱数':>6}{'组':>4}{'★同尺寸异mpm组':>14}"
            f"{'同尺寸最多mpm种':>15}{'满柱指数异质组':>14}{'异质指数占比':>12}")
    print(head)
    print('-' * len(head))
    summaries = []
    for rel in DATASETS:
        p = DATA_DIR / rel
        if not p.exists():
            continue
        s = scan_dataset(p)
        if s is None:
            print(f'{p.name[:44]:<44}  (空)')
            continue
        if s.get('error'):
            print(f"{s['name'][:44]:<44}  (跳过: {s['error'][:30]})")
            continue
        summaries.append(s)
        print(f"{s['name'][:44]:<44}{s['n_boxes']:>6}{s['n_groups']:>4}"
              f"{s['sized_groups']:>14}{s['max_mpm_per_size']:>15}"
              f"{s['het_groups']:>14}{s['het_index_share']*100:>11.1f}%")

    # 总结
    print('\n' + '=' * 78)
    tot_g = sum(s['n_groups'] for s in summaries)
    tot_sg = sum(s['sized_groups'] for s in summaries)
    tot_hg = sum(s['het_groups'] for s in summaries)
    global_max_mpm = max([1] + [s['max_mpm_per_size'] for s in summaries])
    print(f'合计 {len(summaries)} 数据集、{tot_g} 订单组：')
    print(f'  ★精确触发器（同尺寸不同 mpm）命中组数 = {tot_sg}（{tot_sg/tot_g*100:.1f}%），'
          f'全域任一(底面,高)桶最多 mpm 种数 = {global_max_mpm}。')
    print(f'  对照·满柱指数异质（含高度驱动）组数 = {tot_hg}（{tot_hg/tot_g*100:.1f}%）。')
    if global_max_mpm <= 1:
        print('  → 真实数据里「同尺寸→同 mpm」恒成立：balanced_mpm 盲区的精确触发器'
              ' 0% 出现。高度驱动的满柱指数异质虽多，但 668(40%异质箱)实测 11/11，'
              'GCP 已处理。结论：①mpm-aware 凑柱无现实收益；②路由兜底可作未来'
              '插件式保险（仅当出现同尺寸异 mpm 时触发）。')


if __name__ == '__main__':
    main()
