"""小箱不压大箱约束单测（放置函数 + 最终门禁）。"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.utils.helpers import passes_small_box_not_on_larger_constraint
from src.geometry.constraint_validator import (
    validate_pallet_constraints,
    REQUIRED_SUCTION_FIELDS,
)

PALLET = {'length': 1440, 'width': 2240, 'height': 720}
_SUCTION = {f: 1.0 for f in REQUIRED_SUCTION_FIELDS}


def _box(bid, x, y, z, l, w, h, small=False):
    b = {
        'id': bid, 'position': {'x': x, 'y': y, 'z': z},
        'length': l, 'width': w, 'height': h,
        'raw_length': l, 'raw_width': w, 'raw_height': h,
        'weight': 1.0, 'min_pack_multiple': 1, 'is_small_box': small,
        'pallet_dims': PALLET,
    }
    b.update(_SUCTION)
    return b


def _dims(b):
    return {'length': b['length'], 'width': b['width'], 'height': b['height']}


def test_small_box_on_larger_rejected():
    big = _box('big', 0, 0, 0, 700, 530, 240)
    small = _box('small', 0, 0, 240, 175, 265, 120, small=True)
    assert passes_small_box_not_on_larger_constraint(
        small, small['position'], _dims(small), [big]
    ) is False
    print('[PASS] 小箱压大箱 → 拒绝')


def test_small_box_on_floor_ok():
    small = _box('small', 0, 0, 0, 175, 265, 120, small=True)
    assert passes_small_box_not_on_larger_constraint(
        small, small['position'], _dims(small), []
    ) is True
    print('[PASS] 小箱落地 → 通过')


def test_small_box_on_equal_or_smaller_ok():
    base = _box('base', 0, 0, 0, 175, 265, 120, small=True)
    small = _box('s2', 0, 0, 120, 175, 265, 120, small=True)
    assert passes_small_box_not_on_larger_constraint(
        small, small['position'], _dims(small), [base]
    ) is True
    print('[PASS] 小箱压同尺寸 → 通过')


def test_non_small_box_unconstrained():
    big_below = _box('big', 0, 0, 0, 700, 530, 240)
    normal = _box('n', 0, 0, 240, 350, 530, 240, small=False)
    assert passes_small_box_not_on_larger_constraint(
        normal, normal['position'], _dims(normal), [big_below]
    ) is True
    print('[PASS] 非小箱不受约束')


def test_gate_flags_small_box_on_larger():
    big = _box('big', 0, 0, 0, 700, 530, 240)
    small = _box('small', 0, 0, 240, 175, 265, 120, small=True)
    res = validate_pallet_constraints({'packed_items': [big, small]}, PALLET)
    types = [v['type'] for v in res['violations']]
    assert 'small_box_on_larger' in types, types
    print('[PASS] 门禁能识别小箱压大箱')


if __name__ == '__main__':
    test_small_box_on_larger_rejected()
    test_small_box_on_floor_ok()
    test_small_box_on_equal_or_smaller_ok()
    test_non_small_box_unconstrained()
    test_gate_flags_small_box_on_larger()
    print('[PASS] 所有测试通过！')
