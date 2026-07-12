# -*- coding: utf-8 -*-
"""探针：从输出报告 JSON 重放装满压实阶段（_fill_compact）。

用法: python tests/probe/probe_fill_compact_replay.py <报告JSON> [订单号...]
不给订单号时自动扫描全部含失败盘的 (托盘类型, 订单) 组。
验证点：盘数变化、达标数不降、箱子守恒、每组耗时。
"""
import copy
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
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


def replay(report_path: str, orders):
    with io.open(report_path, encoding='utf-8') as fh:
        report = json.load(fh)
    if not orders:
        orders = sorted({
            p.get('sales_order_no') for p in report['pallets']
            if p.get('mpm_status') == 'FAILED'
        })
    for order in orders:
        plans = copy.deepcopy([
            p for p in report['pallets']
            if p.get('sales_order_no') == order
        ])
        if not plans:
            print(f'{order}: 无托盘，跳过')
            continue
        opt = RescueOptimizer(
            pallet_dims=PALLET,
            custom_packer_cls=BeamSearchPacker,
            validate_center_of_mass=validate_center_of_mass,
            constraint_config=ConstraintConfig(),
        )
        n0 = len(plans)
        ids0 = {i['id'] for p in plans for i in p['packed_items']}
        succ0 = sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS')
        fails0 = sorted(
            round(opt._fill_rate(p), 3) for p in plans
            if p.get('mpm_status') == 'FAILED'
        )
        diag = {'fill_compact_merges': 0, 'fill_compact_reason': ''}
        t = time.time()
        opt._fill_compact(plans, TARGET, diag)
        ids1 = {i['id'] for p in plans for i in p['packed_items']}
        succ1 = sum(1 for p in plans if p.get('mpm_status') == 'SUCCESS')
        fails1 = sorted(
            round(opt._fill_rate(p), 3) for p in plans
            if p.get('mpm_status') == 'FAILED'
        )
        print(
            f'{order}: 盘 {n0}->{len(plans)} 达标 {succ0}->{succ1} '
            f'合并 {diag["fill_compact_merges"]} '
            f'守恒={ids0 == ids1} 耗时 {time.time() - t:.1f}s'
        )
        print(f'  失败盘填充 前={fails0}')
        print(f'            后={fails1}')
        assert ids0 == ids1, '箱子守恒必须成立'
        assert succ1 >= succ0, '达标数不得下降'


if __name__ == '__main__':
    replay(sys.argv[1], sys.argv[2:])
