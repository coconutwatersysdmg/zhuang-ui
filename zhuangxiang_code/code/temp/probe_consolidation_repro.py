# -*- coding: utf-8 -*-
"""探针：从 run4 JSON 重建 MN01S 组状态，重复调用互借修复验证确定性+计时。

用法: python temp/probe_consolidation_repro.py <run4_json> [次数]
"""
import copy
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig
from src.geometry import validate_center_of_mass
from src.packing import BeamSearchPacker
from src.rescue import RescueOptimizer

PALLET = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}
TARGET = 192.0

with io.open(sys.argv[1], encoding='utf-8') as f:
    report = json.load(f)
plans0 = [p for p in report['pallets']
          if p.get('sales_order_no') == 'PAIN25450MN01S']
print(f"组内托盘 {len(plans0)}，达标 "
      f"{sum(1 for p in plans0 if p.get('mpm_status') == 'SUCCESS')}")

runs = int(sys.argv[2]) if len(sys.argv) > 2 else 2
for r in range(runs):
    plans = copy.deepcopy(plans0)
    opt = RescueOptimizer(
        pallet_dims=PALLET,
        custom_packer_cls=BeamSearchPacker,
        validate_center_of_mass=validate_center_of_mass,
        constraint_config=ConstraintConfig(),
    )
    # 计时插桩
    orig_extract = opt._extract_target_sets
    orig_repack = opt._repack_pool

    def extract_t(pool, target, deadline, _o=orig_extract, **kw):
        t = time.time()
        sets, rest = _o(pool, target, deadline, **kw)
        print(f"  [extract] {time.time()-t:.1f}s sets={len(sets)} "
              f"rest={len(rest)} 剩余预算={deadline-time.time():.1f}s")
        return sets, rest

    def repack_t(pool, target, **kw, ):
        t = time.time()
        out = orig_repack(pool, target, **kw)
        n = len(out) if out is not None else -1
        print(f"  [repack] {time.time()-t:.1f}s pallets={n} kw_deadline剩余="
              f"{(kw.get('deadline') or 0)-time.time():.1f}s")
        return out

    opt._extract_target_sets = extract_t
    opt._repack_pool = repack_t
    t0 = time.time()
    diag = opt.optimize_failed_by_failed(plans, TARGET)
    print(f"run{r}: {time.time()-t0:.1f}s reason={diag['consolidate_reason']} "
          f"new_pallets={diag.get('consolidate_new_pallets')} "
          f"new_success={diag.get('consolidate_new_success')} "
          f"swap={diag.get('swap_reason')} "
          f"redis={diag.get('redistribute_reason')}")
