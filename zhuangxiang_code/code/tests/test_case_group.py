"""case_group 同组约束测试：归一化、分组隔离、端到端、双层门禁、Excel 可选列。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

import io
from contextlib import redirect_stdout

from src.config import ConstraintConfig
from src.geometry.constraint_validator import validate_pallet_constraints
from src.main.order_processor import OrderProcessor
from src.main.result_formatter import ResultFormatter
from src.utils.case_group import (
    normalize_case_group,
    find_case_group_violation,
    split_case_group_tag,
    tag_sales_order_no,
)

PALLET = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}


def _mk(prefix, count, case_group=None, mpm=2.0):
    """规则箱 350×265×240（每盘 96 箱 ×mpm2=192）。case_group=None 不写字段。"""
    boxes = []
    for i in range(count):
        b = {
            'id': f'{prefix}{i}', 'type': prefix, 'length': 350.0,
            'width': 265.0, 'height': 240.0, 'weight': 1.0,
            'min_pack_multiple': mpm, 'is_small_box': False,
            'pallet_type': 'MH423C', 'sales_order_no': 'ORD1',
            'pallet_dims': dict(PALLET),
        }
        if case_group is not None:
            b['case_group'] = case_group
        boxes.append(b)
    return boxes


def test_normalize():
    """归一化：0/None/NaN/空串/'0'/0.0→0；数值统一整数字符串；字符串去空白。"""
    assert normalize_case_group(None) == 0
    assert normalize_case_group(float('nan')) == 0
    assert normalize_case_group('') == 0
    assert normalize_case_group('  ') == 0
    assert normalize_case_group(0) == 0
    assert normalize_case_group(0.0) == 0
    assert normalize_case_group('0') == 0
    assert normalize_case_group(1) == '1'
    assert normalize_case_group(1.0) == '1'
    assert normalize_case_group('1') == '1'
    assert normalize_case_group('1.0') == '1'
    assert normalize_case_group(' A-2 ') == 'A-2'
    assert normalize_case_group('1.5') == '1.5'
    print('[PASS] normalize_case_group 归一化')


def test_tag_roundtrip():
    """订单号标签：非 0 加后缀可还原；0 不加。"""
    assert tag_sales_order_no('ORD1', 0) == 'ORD1'
    assert tag_sales_order_no('ORD1', None) == 'ORD1'
    tagged = tag_sales_order_no('ORD1', 5)
    assert tagged != 'ORD1'
    assert split_case_group_tag(tagged) == ('ORD1', '5')
    assert split_case_group_tag('ORD1') == ('ORD1', 0)
    print('[PASS] 订单号标签往返')


def test_grouping_isolation():
    """同订单不同 case_group 分成不同组；全 0/缺失时键与历史完全一致。"""
    boxes = _mk('Z', 4) + _mk('X', 4, case_group=1) + _mk('Y', 4, case_group='2')
    grouped = OrderProcessor.group_by_order(boxes)
    assert len(grouped) == 3, f'应 3 组，实际 {len(grouped)}'
    assert ('MH423C', 'ORD1') in grouped, '0 组键必须保持历史原样'
    sizes = sorted(len(v) for v in grouped.values())
    assert sizes == [4, 4, 4]
    # 全 0/缺失 → 单组、键不变（零回归）
    grouped0 = OrderProcessor.group_by_order(_mk('Z', 8))
    assert set(grouped0.keys()) == {('MH423C', 'ORD1')}
    print('[PASS] 分组隔离 + 零回归键')


def test_pallet_gate_purity():
    """整盘门禁：混 case_group 报 case_group_mixed；同组/全 0 不报。"""
    same = _mk('A', 2, case_group=1)
    mixed = _mk('B', 1, case_group=1) + _mk('C', 1, case_group=2)
    with_zero = _mk('D', 1, case_group=1) + _mk('E', 1)  # 非0 与 缺失(=0) 混
    assert find_case_group_violation(same) is None
    assert find_case_group_violation(mixed) is not None
    assert find_case_group_violation(with_zero) is not None
    # 接到 validate_pallet_constraints（无位置字段会另报 missing_position，只看类型）
    gate = validate_pallet_constraints(
        {'packed_items': mixed}, PALLET, constraint_config=ConstraintConfig())
    assert any(v['type'] == 'case_group_mixed' for v in gate['violations'])
    print('[PASS] 整盘门禁纯度检查')


def test_output_gate_uses_input_mapping():
    """输出门禁：按输入 id→case_group 映射判混装（不依赖输出字段透传）。"""
    raw = _mk('A', 1, case_group=1) + _mk('B', 1, case_group=2)
    # 输出侧字段被剥离（只剩 id）也必须被抓到
    pallets = [{
        'pallet_id': 'P1',
        'packed_items': [
            {'id': 'A0', 'length': 350, 'width': 265, 'height': 240},
            {'id': 'B0', 'length': 350, 'width': 265, 'height': 240},
        ],
    }]
    try:
        ResultFormatter.validate_output_quality(raw, pallets)
        raise AssertionError('混装应触发 ValueError')
    except ValueError as exc:
        assert 'case_group_mixed' in str(exc)
    print('[PASS] 输出门禁（输入映射，权威口径）')


def test_end_to_end_run_with_boxes():
    """端到端：一单混 cg=1/cg=2/无约束 各 96 箱 → 3 个纯盘、全达标、守恒、
    订单号还原无内部后缀、盘号唯一、盘级 case_group 写回。"""
    from run_packing import build_workflow
    from src.main.report_persister import NullReportPersister

    boxes = (_mk('G1', 96, case_group=1) + _mk('G2', 96, case_group=2)
             + _mk('G0', 96))
    wf = build_workflow(constraint_config=ConstraintConfig(main_packer='gcp'))
    wf._report_persister = NullReportPersister()
    with redirect_stdout(io.StringIO()):
        report = wf.run_with_boxes(boxes)
    pallets = report['pallets']

    cg_by_id = {b['id']: normalize_case_group(b.get('case_group')) for b in boxes}
    out_ids = []
    for p in pallets:
        groups = {cg_by_id[it['id']] for it in p['packed_items']}
        assert len(groups) <= 1, f"盘 {p['pallet_id']} 混装: {groups}"
        assert p['sales_order_no'] == 'ORD1', '内部后缀必须剥离'
        out_ids.extend(it['id'] for it in p['packed_items'])
    assert sorted(out_ids) == sorted(b['id'] for b in boxes), '守恒'
    ids = [p['pallet_id'] for p in pallets]
    assert len(ids) == len(set(ids)), '盘号唯一'
    succ = sum(1 for p in pallets if p['mpm_status'] == 'SUCCESS')
    assert succ == 3, f'3 组各 1 满盘应全达标，实际 {succ}'
    cg_pallets = {str(p.get('case_group')) for p in pallets if p.get('case_group')}
    assert cg_pallets == {'1', '2'}, f'盘级 case_group 写回: {cg_pallets}'
    print(f'[PASS] 端到端：{len(pallets)} 盘全纯、达标 {succ}/3、守恒、后缀还原')


def test_excel_optional_column():
    """Excel 可选列：带 case_group 列按值读取；无列全 0。

    注：不用 TemporaryDirectory 自动清理——load_boxes 内 pd.ExcelFile 句柄在
    Windows 上可能延迟释放导致清理 PermissionError；改手动 rmtree(ignore_errors)。
    """
    import shutil
    import pandas as pd
    from src.data import load_boxes

    tasks = pd.DataFrame({
        '箱子序号': ['B1', 'B2', 'B3'],
        'Box类型': ['T1', 'T1', 'T1'],
        '总重量': [1.0, 1.0, 1.0],
        '销售订单号': ['O1', 'O1', 'O1'],
        '箱子长': [350, 350, 350], '箱子宽': [265, 265, 265],
        '箱子高': [240, 240, 240],
        'Case类型': ['MH423C'] * 3,
        '托盘长': [1440] * 3, '托盘宽': [2240] * 3, '托盘高': [720] * 3,
        'case_group': [1, None, 'A'],
    })
    bms = pd.DataFrame({'包装规格代码': ['T1'], '最小包装量的倍数': [2]})

    td = tempfile.mkdtemp()
    try:
        fp = Path(td) / 't.xlsx'
        with pd.ExcelWriter(fp) as w:
            tasks.to_excel(w, sheet_name='最终挑选结果', index=False)
            bms.to_excel(w, sheet_name='包装物料主数据(BMS)', index=False)
        with redirect_stdout(io.StringIO()):
            loaded = load_boxes(str(fp))
        got = {b['id']: b['case_group'] for b in loaded}
        assert got == {'B1': '1', 'B2': 0, 'B3': 'A'}, got

        # 无列 → 全 0
        fp2 = Path(td) / 't2.xlsx'
        with pd.ExcelWriter(fp2) as w:
            tasks.drop(columns=['case_group']).to_excel(
                w, sheet_name='最终挑选结果', index=False)
            bms.to_excel(w, sheet_name='包装物料主数据(BMS)', index=False)
        with redirect_stdout(io.StringIO()):
            loaded = load_boxes(str(fp2))
        assert all(b['case_group'] == 0 for b in loaded)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print('[PASS] Excel 可选列（带列/缺列）')


if __name__ == '__main__':
    test_normalize()
    test_tag_roundtrip()
    test_grouping_isolation()
    test_pallet_gate_purity()
    test_output_gate_uses_input_mapping()
    test_end_to_end_run_with_boxes()
    test_excel_optional_column()
    print('\n[PASS] case_group 全部测试通过！')
