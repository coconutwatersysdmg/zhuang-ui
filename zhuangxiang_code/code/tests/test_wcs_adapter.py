"""WCS 适配层测试：输入展开、输出 case 结构、排序、口径、端到端往返。

对照《北自柳州五菱项目接口文档》v1.5 的接口 1/2 示例与
`docs/WCS接口对接分析与算法接入说明.md` 的映射表。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.adapter import (
    build_stock_request,
    default_pallet_dims_map,
    report_to_plan_result,
    stock_to_boxes,
)
from src.config import ConstraintConfig

MH423C = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}


def _entry(box_type, n, L, W, H, order='ORD1', prio=1, cg='0',
           pc=10791358, case_type='MH423C', weight=1.0):
    """接口 1 库存条目（字段名/类型对照接口文档 4.1.4）。"""
    return {
        'length': float(L), 'width': float(W), 'height': float(H),
        'target_num': n, 'weight': weight, 'box_type': box_type,
        'case_type': case_type, 'case_group': cg,
        'product_code': pc, 'order_id': order, 'priority': prio,
    }


def _run(boxes):
    from run_packing import build_workflow
    from src.main.report_persister import NullReportPersister
    wf = build_workflow(constraint_config=ConstraintConfig(main_packer='gcp'))
    wf._report_persister = NullReportPersister()
    with redirect_stdout(io.StringIO()):
        return wf.run_with_boxes(boxes)


def test_stock_request():
    """接口 1 请求体：msgid 32 位 hex。"""
    req = build_stock_request()
    assert set(req) == {'msgtime', 'msgid'}
    assert len(req['msgid']) == 32 and int(req['msgid'], 16) >= 0
    print('[PASS] 接口1 请求体')


def test_stock_expansion_and_mapping():
    """聚合库存展开：数量/字段映射/id 唯一/指数查表/透传/归一化。"""
    stock = [
        _entry('T1', 3, 350, 265, 240, order='A', prio=1, cg='0', pc=111),
        _entry('T2', 2, 700, 530, 360, order='B', prio=2, cg='5', pc=222),
    ]
    boxes = stock_to_boxes(stock, bms_map={'T1': 2.0})  # T2 不在 BMS
    assert len(boxes) == 5, '3+2 展开'
    ids = [b['id'] for b in boxes]
    assert len(ids) == len(set(ids)), 'id 全局唯一'
    b1 = boxes[0]
    assert (b1['type'], b1['pallet_type'], b1['sales_order_no']) == ('T1', 'MH423C', 'A')
    assert b1['min_pack_multiple'] == 2.0 and b1['pallet_dims'] == MH423C
    assert b1['case_group'] == 0 and b1['product_code'] == 111 and b1['priority'] == 1
    b2 = boxes[-1]
    assert b2['min_pack_multiple'] == 0.0, 'BMS 缺失按 0（excel_loader 同口径）'
    assert b2['case_group'] == '5', 'case_group 归一化直传'
    assert all('is_small_box' in b and 'volume' in b for b in boxes)
    print('[PASS] 库存展开与字段映射')


def test_unknown_case_type_raises():
    """未配置托盘尺寸的 case_type（如 MH110 未配）→ 快速失败。"""
    try:
        stock_to_boxes([_entry('T1', 1, 350, 265, 240, case_type='MH110')],
                       bms_map={})
        raise AssertionError('应抛 ValueError')
    except ValueError as exc:
        assert 'MH110' in str(exc)
    print('[PASS] 未知 case_type 快速失败')


def test_pallet_dims_map_from_yaml():
    """默认托盘尺寸映射：yaml 的 MH423C 生效。"""
    dims = default_pallet_dims_map()
    assert dims['MH423C'] == MH423C
    print('[PASS] yaml pallets 映射')


def test_end_to_end_roundtrip():
    """端到端往返：库存→展开→算法→接口2 结构逐字段校验。

    订单 A（priority=2）与 B（priority=1）各 96 箱 350×265×240 mpm2
    （每盘 32/层 ×3 层 = 96 箱 = 192 达标）→ 2 个 case；B 优先排前。
    """
    stock = [
        _entry('T1', 96, 350, 265, 240, order='A', prio=2, pc=111),
        _entry('T1', 96, 350, 265, 240, order='B', prio=1, pc=222),
    ]
    boxes = stock_to_boxes(stock, bms_map={'T1': 2.0})
    report = _run(boxes)
    result = report_to_plan_result(report)
    cases = result.cases

    assert len(cases) == 2
    assert [c['box_index'] for c in cases] == [1, 2], 'box_index 从 1 连续'
    assert cases[0]['order_id'] == 'B' and cases[1]['order_id'] == 'A', \
        'priority 小者优先（TODO §8-3 方向）'
    for c in cases:
        # case 级字段
        assert len(c['box_unique_id']) == 32 and int(c['box_unique_id'], 16) >= 0
        assert c['case_type'] == 'MH423C' and c['case_group'] == '0'
        assert abs(c['total_height'] - 720.0) < 1e-6, '3 层×240=720'
        # layers / cartons
        assert len(c['layers']) == 3, '按 z 分 3 层'
        cartons = [ct for ly in c['layers'] for ct in ly['cartons']]
        assert len(cartons) == 96
        assert [ct['seq'] for ct in cartons] == list(range(1, 97)), \
            'seq 连续 1..N（(z,y,x) 码垛序）'
        assert {ct['layer_id'] for ct in c['layers'][0]['cartons']} == {1}
        # ⚠️ 真实尺寸口径：350 而非 +2mm 的 352
        assert all(ct['length'] == 350.0 and ct['width'] == 265.0
                   and ct['height'] == 240.0 for ct in cartons)
        assert all(ct['product_code'] in (111, 222) for ct in cartons)
    # 执行层映射：含坐标的完整方案
    assert set(result.plan_by_unique_id) == {c['box_unique_id'] for c in cases}
    any_plan = next(iter(result.plan_by_unique_id.values()))
    assert any_plan['packed_items'][0].get('position') is not None
    print('[PASS] 端到端往返（排序/层/seq/尺寸口径/映射）')


def test_case_group_passthrough_to_case():
    """非 0 case_group：库存→算法→case 级 case_group 直传。"""
    stock = [_entry('T1', 96, 350, 265, 240, cg='7', pc=333)]
    boxes = stock_to_boxes(stock, bms_map={'T1': 2.0})
    result = report_to_plan_result(_run(boxes))
    assert len(result.cases) == 1 and result.cases[0]['case_group'] == '7'
    print('[PASS] case_group 非 0 直传')


def test_failed_pallet_toggle():
    """未达标盘：默认输出（守恒）；include_failed=False 则过滤。"""
    stock = [_entry('T1', 24, 350, 265, 240, pc=444)]  # 24×2=48 < 192
    boxes = stock_to_boxes(stock, bms_map={'T1': 2.0})
    report = _run(boxes)
    full = report_to_plan_result(report)
    assert len(full.cases) == 1, '默认含未达标盘（TODO §8-5）'
    only_ok = report_to_plan_result(report, include_failed=False)
    assert only_ok.cases == [] and only_ok.plan_by_unique_id == {}
    print('[PASS] 未达标盘开关')


def test_empty_inputs():
    """空库存/空报告 → 空数组（接口允许 []）。"""
    assert stock_to_boxes([], bms_map={}) == []
    r = report_to_plan_result(None)
    assert r.cases == [] and r.plan_by_unique_id == {}
    print('[PASS] 空输入/空报告')


if __name__ == '__main__':
    test_stock_request()
    test_stock_expansion_and_mapping()
    test_unknown_case_type_raises()
    test_pallet_dims_map_from_yaml()
    test_end_to_end_roundtrip()
    test_case_group_passthrough_to_case()
    test_failed_pallet_toggle()
    test_empty_inputs()
    print('\n[PASS] WCS 适配层全部测试通过！')
