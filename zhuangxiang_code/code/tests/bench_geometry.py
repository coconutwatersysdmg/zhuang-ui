"""数值版 support 对 Shapely 参照的等价校验 + 计时。"""
import sys
import time
import random
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from shapely.geometry import box as shapely_box
from shapely.ops import unary_union

from src.geometry.support import calculate_direct_supported_area


def shapely_supported_area(point, dims, placed_boxes):
    """原 Shapely 实现的独立参照副本。"""
    if point['z'] == 0:
        return dims['length'] * dims['width']
    upper = shapely_box(
        point['x'], point['y'],
        point['x'] + dims['length'], point['y'] + dims['width'],
    )
    foot = [
        shapely_box(
            b['position']['x'], b['position']['y'],
            b['position']['x'] + b['length'], b['position']['y'] + b['width'],
        )
        for b in placed_boxes
        if abs((b['position']['z'] + b['height']) - point['z']) < 1e-5
    ]
    if not foot:
        return 0.0
    return upper.intersection(unary_union(foot)).area


def _rand_box(rng, z):
    return {
        'position': {'x': rng.randint(0, 700), 'y': rng.randint(0, 500), 'z': z},
        'length': rng.randint(100, 600),
        'width': rng.randint(100, 600),
        'height': rng.choice([200, 240, 300, 480]),
    }


def test_support_equivalence():
    rng = random.Random(12345)
    max_rel = 0.0
    for _ in range(20000):
        z = rng.choice([0, 240, 480])
        placed = [_rand_box(rng, rng.choice([0, 240, 480]))
                  for _ in range(rng.randint(0, 8))]
        dims = {'length': rng.randint(100, 600),
                'width': rng.randint(100, 600), 'height': 240}
        point = {'x': rng.randint(0, 700), 'y': rng.randint(0, 500), 'z': z}
        a = calculate_direct_supported_area(point, dims, placed)
        b = shapely_supported_area(point, dims, placed)
        base = dims['length'] * dims['width']
        rel = abs(a - b) / base if base > 0 else abs(a - b)
        max_rel = max(max_rel, rel)
        assert rel < 1e-6, (
            '不等价 numeric=%r shapely=%r point=%r dims=%r placed=%r'
            % (a, b, point, dims, placed)
        )
    print('[OK] support 等价: 20000 例, 最大相对误差=%.2e' % max_rel)


def bench_support():
    rng = random.Random(7)
    cases = []
    for _ in range(2000):
        placed = [_rand_box(rng, rng.choice([0, 240, 480]))
                  for _ in range(rng.randint(5, 30))]
        point = {'x': rng.randint(0, 700), 'y': rng.randint(0, 500), 'z': 240}
        dims = {'length': rng.randint(100, 600),
                'width': rng.randint(100, 600), 'height': 240}
        cases.append((point, dims, placed))

    t = time.time()
    for point, dims, placed in cases:
        calculate_direct_supported_area(point, dims, placed)
    dn = time.time() - t

    t = time.time()
    for point, dims, placed in cases:
        shapely_supported_area(point, dims, placed)
    ds = time.time() - t
    print('[BENCH] 2000 例: 数值=%.1fms  Shapely=%.1fms  加速=%.1fx'
          % (dn * 1e3, ds * 1e3, ds / dn if dn > 0 else float('inf')))


if __name__ == '__main__':
    test_support_equivalence()
    bench_support()
