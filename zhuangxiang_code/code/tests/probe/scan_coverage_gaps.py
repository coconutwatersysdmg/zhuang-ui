"""覆盖缺口探测（B）：B1 MH110(target=32)、B2 case_group 达标率、B3 失败盘填充上界。

此前泛化探测全部基于 MH423C/192、无 case_group、只看达标不对照填充上界。
本脚本闭合这三个验证缺口（探测算法行为，不改算法）。

用法：cd code && python -m tests.probe.scan_coverage_gaps
注意：MH110 用配置模板的示例尺寸 1200×800×1450（真实业务尺寸待确认——模板里
MH423C 的尺寸同样与真实 1440×2240×720 不符，故模板值仅作探测用）。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional

_CODE_DIR = Path(__file__).resolve().parent.parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from run_packing import build_workflow  # noqa: E402
from src.config import ConstraintConfig  # noqa: E402
from src.main.report_persister import NullReportPersister  # noqa: E402
from src.utils.case_group import normalize_case_group  # noqa: E402

MH423C = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}
# 配置模板示例尺寸（src/config/loader.py create_default_config）；真实尺寸待业务确认
MH110 = {'length': 1200.0, 'width': 800.0, 'height': 1450.0}


def _mk(prefix: str, count: int, L: float, W: float, H: float, mpm: float,
        pallet_type: str = 'MH423C', pallet: Optional[Dict] = None,
        order: str = 'ORD1', case_group=None) -> List[Dict]:
    pallet = pallet or MH423C
    out = []
    for i in range(count):
        b = {
            'id': f'{prefix}{i}', 'type': prefix, 'length': float(L),
            'width': float(W), 'height': float(H), 'weight': 1.0,
            'min_pack_multiple': float(mpm), 'is_small_box': False,
            'pallet_type': pallet_type, 'sales_order_no': order,
            'pallet_dims': dict(pallet),
        }
        if case_group is not None:
            b['case_group'] = case_group
        out.append(b)
    return out


def _run(boxes: List[Dict]) -> Dict:
    wf = build_workflow(constraint_config=ConstraintConfig(main_packer='gcp'))
    wf._report_persister = NullReportPersister()
    with redirect_stdout(io.StringIO()):
        return wf.run_with_boxes(boxes)


def _conserved(report: Dict, boxes: List[Dict]) -> bool:
    out = sorted(b['id'] for p in report['pallets'] for b in p['packed_items'])
    return out == sorted(b['id'] for b in boxes)


def b1_mh110() -> None:
    """B1: MH110（target=32，1200×800×1450 示例尺寸）——算法各路径是否正确处理。

    箱型 298×398×350 mpm1：每层 4×2=8、4 层 → 每盘 32 箱 ×1 = 32 = target。
    N=3 盘已知最优。另测 MH423C+MH110 混单（分组键含托盘类型应各自独立）。
    """
    print('=' * 70 + '\nB1  MH110 (target=32, 示例尺寸 1200×800×1450)\n' + '=' * 70)
    per = int(MH110['length'] // 300) * int(MH110['width'] // 400)  # 4×2=8
    layers = int(MH110['height'] // 350)  # 4
    assert per * layers * 1 == 32, (per, layers)

    n = 3
    boxes = _mk('M', per * layers * n, 298, 398, 350, 1,
                pallet_type='MH110', pallet=MH110)
    rep = _run(boxes)
    ov = rep['summary']['overall']
    fills = [round(float(p.get('fill_rate', 0)), 3) for p in rep['pallets']]
    print(f"  纯 MH110 订单: 达标 {ov['success_pallets']}/{n} (总盘 "
          f"{ov['total_pallets']})  守恒={'Y' if _conserved(rep, boxes) else 'N'}"
          f"  各盘填充={fills}")

    mixed = boxes + _mk('A', 96, 350, 265, 240, 2)  # + 1 盘 MH423C
    rep2 = _run(mixed)
    by_type: Dict[str, List[int]] = {}
    for p in rep2['pallets']:
        by_type.setdefault(p['pallet_type'], [0, 0])
        by_type[p['pallet_type']][0] += 1
        if p['mpm_status'] == 'SUCCESS':
            by_type[p['pallet_type']][1] += 1
    print(f"  混托盘类型订单: "
          + '，'.join(f'{t}: 达标{v[1]}/{v[0]}' for t, v in sorted(by_type.items()))
          + f"  守恒={'Y' if _conserved(rep2, mixed) else 'N'}")
    exp = ov['success_pallets'] == n and _conserved(rep, boxes)
    print(f"  → B1 结论: {'MH110 路径正常(达最优+守恒)' if exp else '★发现问题,需排查'}")


def b2_case_group() -> None:
    """B2: case_group 约束下的达标率——多非 0 组各自可达标是否全部摆到最优。

    场景A: 5 个非 0 组 + 1 个 0 组，各 1 满盘规则箱 → 已知最优 6/6。
    场景B: cg×特性组合——cg1=旋转敏感(530×350 坏朝向)、cg2=高密度(358×558)、
           cg3=规则 → 每组各自路由应正确，已知最优 3/3。
    场景C: cg 小组凑不满(24 箱 mpm2=48<192) → 应独立 FAILED 盘、装满自己、
           填充→上界(24箱体积/盘)。
    """
    print('\n' + '=' * 70 + '\nB2  case_group 达标率探测\n' + '=' * 70)

    # A: 6 组各 1 满盘
    boxes_a: List[Dict] = _mk('Z', 96, 350, 265, 240, 2)  # cg=0
    for g in range(1, 6):
        boxes_a += _mk(f'C{g}_', 96, 350, 265, 240, 2, case_group=g)
    rep = _run(boxes_a)
    ov = rep['summary']['overall']
    cg_by_id = {b['id']: normalize_case_group(b.get('case_group')) for b in boxes_a}
    pure = all(
        len({cg_by_id[it['id']] for it in p['packed_items']}) <= 1
        for p in rep['pallets'])
    print(f"  A 六组各1满盘: 达标 {ov['success_pallets']}/6 (总盘 {ov['total_pallets']})"
          f"  全盘纯={'Y' if pure else 'N'}  守恒={'Y' if _conserved(rep, boxes_a) else 'N'}")

    # B: cg × 特性（旋转敏感/高密度/规则）
    boxes_b = (_mk('R', 48, 530, 350, 240, 4, case_group=1)      # 旋转敏感 1 盘
               + _mk('D', 48, 358, 558, 240, 4, case_group=2)    # 高密度 1 盘
               + _mk('G', 96, 350, 265, 240, 2, case_group=3))   # 规则 1 盘
    rep_b = _run(boxes_b)
    ov_b = rep_b['summary']['overall']
    detail = {str(p.get('case_group')): p['mpm_status'] for p in rep_b['pallets']}
    print(f"  B cg×特性(旋转/高密度/规则): 达标 {ov_b['success_pallets']}/3 "
          f"(总盘 {ov_b['total_pallets']})  各组状态={detail}"
          f"  守恒={'Y' if _conserved(rep_b, boxes_b) else 'N'}")

    # C: cg 小组凑不满 → 装满测试
    boxes_c = _mk('S', 24, 350, 265, 240, 2, case_group=9) + _mk('F', 96, 350, 265, 240, 2)
    rep_c = _run(boxes_c)
    small = [p for p in rep_c['pallets'] if str(p.get('case_group')) == '9']
    box_vol = 350 * 265 * 240 * 24
    ub = box_vol / (MH423C['length'] * MH423C['width'] * MH423C['height'])
    got = float(small[0].get('fill_rate', 0)) if small else -1
    print(f"  C 小组(48指数<192): 盘数={len(small)} 状态="
          f"{small[0]['mpm_status'] if small else '-'} 填充={got:.3f}"
          f" (上界={ub:.3f}, 比={got/ub:.1%})")

    okA = ov['success_pallets'] == 6 and pure
    okB = ov_b['success_pallets'] == 3
    okC = bool(small) and len(small) == 1 and got / ub > 0.999
    print(f"  → B2 结论: A{'✓' if okA else '✗'} B{'✓' if okB else '✗'} "
          f"C{'✓' if okC else '✗'}"
          + ('  全部达最优' if okA and okB and okC else '  ★有未达最优项,见上'))


def b3_fill_upper_bound() -> None:
    """B3: 失败盘填充率 vs 已知几何上界（把 S/N 方法论移植到"尽量装满"）。

    三个不可达标构造（单一箱型 mpm 压到永不达标），已知最优装法与上界填充：
      ① 整盘量: 24×(700×530×240) 恰满 1 盘 → 上界 92.0%
      ② 高密度: 48×(358×558×240) 恰满 1 盘 → 上界 99.1%
      ③ 超一盘量: 36×(700×530×240) → 最优 = 1 满盘(92.0%) + 1 半盘(46.0%)，
         总体积/2 盘 → 平均上界 69.0%，且最满盘应 =92.0%
    """
    print('\n' + '=' * 70 + '\nB3  失败盘填充率上界对照（不可达标订单的"尽量装满"）\n' + '=' * 70)
    pallet_vol = MH423C['length'] * MH423C['width'] * MH423C['height']

    cases = [
        ('① 整盘量 24×700×530×240', _mk('U', 24, 700, 530, 240, 1), 1,
         [24 * 700 * 530 * 240 / pallet_vol]),
        ('② 高密度 48×358×558×240', _mk('V', 48, 358, 558, 240, 1), 1,
         [48 * 358 * 558 * 240 / pallet_vol]),
        ('③ 超一盘 36×700×530×240', _mk('W', 36, 700, 530, 240, 1), 2,
         [24 * 700 * 530 * 240 / pallet_vol, 12 * 700 * 530 * 240 / pallet_vol]),
    ]
    all_ok = True
    for name, boxes, n_opt, ubs in cases:
        rep = _run(boxes)
        fills = sorted((float(p.get('fill_rate', 0)) for p in rep['pallets']),
                       reverse=True)
        ubs = sorted(ubs, reverse=True)
        cons = _conserved(rep, boxes)
        n_ok = len(fills) == n_opt
        # 逐盘对照上界（盘数相同才逐盘比；总体积守恒下盘数=n_opt ⇒ 分布最优看最满盘）
        ratio = (sum(fills) / sum(ubs)) if ubs else 0
        top_ratio = fills[0] / ubs[0] if fills and ubs else 0
        ok = n_ok and cons and top_ratio > 0.999 and ratio > 0.999
        all_ok = all_ok and ok
        print(f"  {name}: 盘数 {len(fills)}(最优{n_opt}) 各盘填充="
              f"{[round(f, 3) for f in fills]} 上界={[round(u, 3) for u in ubs]}"
              f" 总比={ratio:.1%} 守恒={'Y' if cons else 'N'} {'✓' if ok else '★未达上界'}")
    print(f"  → B3 结论: {'失败盘全部装到几何上界(装满已最优)' if all_ok else '★有未达上界项,见上'}")


if __name__ == '__main__':
    b1_mh110()
    b2_case_group()
    b3_fill_upper_bound()
