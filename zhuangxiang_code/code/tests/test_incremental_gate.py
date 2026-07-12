"""增量门禁对整盘校验器（几何部分）的等价单测。"""
import sys
import random
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.geometry.constraint_validator import (
    REQUIRED_SUCTION_FIELDS,
    validate_pallet_constraints,
)
from src.packing.incremental_gate import incremental_pallet_ok

_SUCTION_STUB = {field: 1.0 for field in REQUIRED_SUCTION_FIELDS}


def _rand_box(rng, pallet_dims, placed):
    length = rng.choice([300, 350, 500, 600, 700])
    width = rng.choice([300, 400, 500, 530])
    height = rng.choice([240, 300, 480])
    z_choices = [0] + [
        box['position']['z'] + box['height'] for box in placed
    ]
    z = rng.choice(z_choices)
    x = rng.randint(0, max(0, int(pallet_dims['length'] - length)))
    y = rng.randint(0, max(0, int(pallet_dims['width'] - width)))
    box = {
        'id': 'b%d' % rng.randint(0, 10 ** 9),
        'position': {'x': x, 'y': y, 'z': z},
        'length': length, 'width': width, 'height': height,
        'raw_length': length, 'raw_width': width, 'raw_height': height,
        'weight': rng.uniform(1, 5),
        'min_pack_multiple': rng.choice([4, 8, 16]),
    }
    box.update(_SUCTION_STUB)
    return box


def _full_geom_ok(items, pallet_dims):
    """整盘校验，但忽略重心违例（重心由 top-up 调用方单独判定）。"""
    result = validate_pallet_constraints({'packed_items': items}, pallet_dims)
    if result['is_valid']:
        return True
    return all(
        violation.get('type') == 'center_of_mass'
        for violation in result['violations']
    )


def test_incremental_gate_equivalence():
    rng = random.Random(99)
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    checked = 0
    mismatched = 0
    nontrivial_accept = 0
    for _ in range(5000):
        # 用整盘校验器增量构造一个几何合法的 placed
        placed = []
        for _ in range(rng.randint(0, 10)):
            candidate = _rand_box(rng, pallet_dims, placed)
            if _full_geom_ok(placed + [candidate], pallet_dims):
                placed.append(candidate)
        new_box = _rand_box(rng, pallet_dims, placed)
        full_verdict = _full_geom_ok(placed + [new_box], pallet_dims)
        incremental_verdict = incremental_pallet_ok(
            new_box, placed, pallet_dims
        )
        checked += 1
        if full_verdict and placed:
            nontrivial_accept += 1
        if full_verdict != incremental_verdict:
            mismatched += 1
            if mismatched <= 5:
                print(
                    'MISMATCH full=%s inc=%s placed=%d new=%r'
                    % (full_verdict, incremental_verdict,
                       len(placed), new_box['position'])
                )
    assert mismatched == 0, '%d/%d 例不等价' % (mismatched, checked)
    assert nontrivial_accept > 100, (
        '非平凡接受样本过少(%d)，测试覆盖不足' % nontrivial_accept
    )
    print(
        '[OK] 增量门禁等价: %d 例全部一致（其中非平凡接受 %d 例）'
        % (checked, nontrivial_accept)
    )


if __name__ == '__main__':
    test_incremental_gate_equivalence()
    print('[PASS] incremental gate tests')
