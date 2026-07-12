# -*- coding: utf-8 -*-
"""消融对照：monkeypatch 回旧严格 gap 语义（四方向最近正间隙必须 < max_gap），
跑 chain 数据，与锚定语义结果对比，归因达标数差异。"""
import sys
from pathlib import Path

_CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_CODE))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

import src.geometry.gap_checker as gc
from src.geometry.overlap import axis_overlap_len


def _old_strict_gap(point, dims, raw_dims, placed_boxes, max_gap=6.0,
                    pallet_dims=None):
    """旧语义：X/Y 四方向最近正间隙全部 < max_gap（忽略 pallet_dims）。"""
    if not isinstance(raw_dims, dict):
        raw_dims = dims
    eps = 1e-9
    x_min = float(point['x']); x_max = x_min + float(raw_dims['length'])
    y_min = float(point['y']); y_max = y_min + float(raw_dims['width'])
    z_min = float(point['z']); z_max = z_min + float(raw_dims['height'])
    nearest = {'x_min': None, 'x_max': None, 'y_min': None, 'y_max': None}
    for pb in placed_boxes:
        pos = pb.get('position')
        if not pos:
            continue
        rl = float(pb.get('raw_length', pb.get('length', 0)) or 0)
        rw = float(pb.get('raw_width', pb.get('width', 0)) or 0)
        rh = float(pb.get('raw_height', pb.get('height', 0)) or 0)
        px0, px1 = float(pos['x']), float(pos['x']) + rl
        py0, py1 = float(pos['y']), float(pos['y']) + rw
        pz0, pz1 = float(pos['z']), float(pos['z']) + rh
        if axis_overlap_len(z_min, z_max, pz0, pz1) <= eps:
            continue
        if axis_overlap_len(y_min, y_max, py0, py1) > eps:
            lg, rg = x_min - px1, px0 - x_max
            if lg >= -eps:
                nearest['x_min'] = lg if nearest['x_min'] is None else min(nearest['x_min'], lg)
            if rg >= -eps:
                nearest['x_max'] = rg if nearest['x_max'] is None else min(nearest['x_max'], rg)
        if axis_overlap_len(x_min, x_max, px0, px1) > eps:
            fg, bg = y_min - py1, py0 - y_max
            if fg >= -eps:
                nearest['y_min'] = fg if nearest['y_min'] is None else min(nearest['y_min'], fg)
            if bg >= -eps:
                nearest['y_max'] = bg if nearest['y_max'] is None else min(nearest['y_max'], bg)
    return all(g is None or g < max_gap - eps for g in nearest.values())


# 全局替换（含各模块 from-import 引用）
gc.passes_box_gap_constraint = _old_strict_gap
import src.geometry.constraint_validator as cv
cv.passes_box_gap_constraint = _old_strict_gap
import src.packing.sanitizer as sz
sz.passes_box_gap_constraint = _old_strict_gap
import src.packing.direct_layer_packer as dl
dl.passes_box_gap_constraint = _old_strict_gap
import src.packing.incremental_gate as ig
ig.passes_box_gap_constraint = _old_strict_gap

import io
import time
from collections import defaultdict
from contextlib import redirect_stdout

from run_packing import build_workflow
from src.config.constants import DATA_DIR
from src.data import load_boxes
from src.main.report_persister import NullReportPersister

name = sys.argv[1] if len(sys.argv) > 1 else 'selected_5000_full_regular_10_0_chain.xlsx'
path = DATA_DIR / name
with redirect_stdout(io.StringIO()):
    boxes = load_boxes(str(path))
wf = build_workflow()
wf._report_persister = NullReportPersister()
t0 = time.time()
buf = io.StringIO()
with redirect_stdout(buf):
    report = wf.run_with_boxes(boxes)
ov = report['summary']['overall']
print(f'== [旧严格gap语义] {name} ==')
print(f"总盘 {ov['total_pallets']}  达标 {ov['success_pallets']}  "
      f"未达标 {ov['failed_pallets']}  用时 {time.time()-t0:.0f}s")
g = defaultdict(lambda: {'S': 0, 'F': 0})
for p in report['pallets']:
    k = (p.get('pallet_type'), p.get('sales_order_no'))
    g[k]['S' if p.get('mpm_status') == 'SUCCESS' else 'F'] += 1
for k, v in sorted(g.items()):
    print(f"  {k[0]}|{k[1]}: S={v['S']} F={v['F']}")
