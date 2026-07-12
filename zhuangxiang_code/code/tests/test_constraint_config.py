"""约束统一配置系统单测：数据结构、加载、注入、开关生效。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

from src.config import ConstraintConfig, ConfigLoader
from src.geometry.constraint_validator import validate_pallet_constraints


def _box(box_id, x, y, z, l, w, h, weight=10.0, is_small=False):
    """构造一个带必要字段的已放置箱（含吸盘字段以通过 require_suction）。"""
    b = {
        'id': box_id, 'position': {'x': x, 'y': y, 'z': z},
        'length': l, 'width': w, 'height': h,
        'raw_length': l, 'raw_width': w, 'raw_height': h,
        'weight': weight, 'is_small_box': is_small,
        'pallet_dims': {'length': 1200, 'width': 1000, 'height': 1450},
    }
    for f in (
        'suction_box_corner', 'suction_cup_corner', 'suction_orientation',
        'suction_cup_x_size', 'suction_cup_y_size',
        'suction_rect_x_min', 'suction_rect_x_max',
        'suction_rect_y_min', 'suction_rect_y_max',
    ):
        b[f] = 0
    return b


def test_defaults():
    c = ConstraintConfig()
    assert c.max_box_gap_mm == 6.0
    assert c.support_ratio_threshold == 0.8
    assert abs(c.center_of_mass_tolerance - 1.0 / 3.0) < 1e-12
    assert c.suction_reachability_enabled
    assert c.small_box_below_enabled
    assert c.same_size_heavier_below_enabled
    assert c.height_multiple_layering_enabled
    assert c.suction_cup_length == 600.0 and c.suction_cup_width == 800.0
    print('[PASS] 默认值符合现状')


def test_from_dict_tolerant():
    c = ConstraintConfig.from_dict(
        {'max_box_gap_mm': 8.0, 'small_box_below_enabled': False, 'X': 1}
    )
    assert c.max_box_gap_mm == 8.0
    assert c.small_box_below_enabled is False
    assert c.support_ratio_threshold == 0.8  # 未提供 → 默认
    assert ConstraintConfig.from_dict(None).max_box_gap_mm == 6.0
    assert ConstraintConfig.from_dict({}).support_ratio_threshold == 0.8
    print('[PASS] from_dict 容错（未知键忽略 / 缺省回退）')


def test_frozen():
    c = ConstraintConfig()
    try:
        c.max_box_gap_mm = 9.0  # type: ignore
    except Exception:
        print('[PASS] 配置不可变（frozen）')
        return
    raise AssertionError('配置应为 frozen，不可赋值')


def test_loader_robot_alias():
    """旧 robot 段的吸盘几何应能回填到约束配置。"""
    loader = ConfigLoader.from_dict(
        {'robot': {'cup_length': 650.0, 'reachability_enabled': False}}
    )
    cc = loader.load_constraint_config()
    assert cc.suction_cup_length == 650.0
    assert cc.suction_reachability_enabled is False
    print('[PASS] loader 兼容旧 robot 段')


def test_gate_small_box_switch():
    """小箱在下开关：关闭后门禁不再判违例。"""
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    # 小箱(体积小)压在大箱(体积大)上 → 违反「小箱在下」
    big = _box('big', 0, 0, 0, 400, 400, 100, weight=5.0)
    small = _box('small', 0, 0, 100, 200, 200, 100, weight=20.0, is_small=True)
    plan = {'packed_items': [big, small]}

    on = validate_pallet_constraints(
        plan, pallet_dims, constraint_config=ConstraintConfig()
    )
    types_on = {v['type'] for v in on['violations']}
    assert 'small_box_on_larger' in types_on, types_on

    off = validate_pallet_constraints(
        plan, pallet_dims,
        constraint_config=ConstraintConfig(small_box_below_enabled=False),
    )
    types_off = {v['type'] for v in off['violations']}
    assert 'small_box_on_larger' not in types_off, types_off
    print('[PASS] 门禁「小箱在下」开关生效（开→拦，关→放行）')


def test_gate_gap_value_configurable():
    """间隙阈值可配：调大阈值会把原本合规的间隙判成违例。"""
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    # 两箱 X 方向留 5mm 间隙
    a = _box('a', 0, 0, 0, 300, 300, 100)
    b = _box('b', 305, 0, 0, 300, 300, 100)
    plan = {'packed_items': [a, b]}

    # 默认 6mm：5mm 间隙 < 6mm → 视为「紧贴」合规
    d6 = validate_pallet_constraints(
        plan, pallet_dims, constraint_config=ConstraintConfig()
    )
    assert not any(v['type'] == 'gap' for v in d6['violations'])

    # 调到 4mm：5mm 间隙 > 4mm → 该侧无合规邻居 → gap 违例
    d4 = validate_pallet_constraints(
        plan, pallet_dims,
        constraint_config=ConstraintConfig(max_box_gap_mm=4.0),
    )
    assert any(v['type'] == 'gap' for v in d4['violations'])
    print('[PASS] 门禁间隙阈值可配（6→4mm 改变判定）')


def test_gate_com_tolerance_configurable():
    """重心偏差可配：偏置布局在严格阈值下变不稳定。"""
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    # 单个重箱压在托盘一角 → 重心明显偏移
    heavy = _box('h', 0, 0, 0, 300, 300, 100, weight=100.0)
    plan = {'packed_items': [heavy]}

    loose = validate_pallet_constraints(
        plan, pallet_dims,
        constraint_config=ConstraintConfig(center_of_mass_tolerance=0.5),
    )
    strict = validate_pallet_constraints(
        plan, pallet_dims,
        constraint_config=ConstraintConfig(center_of_mass_tolerance=0.05),
    )
    assert not any(v['type'] == 'center_of_mass' for v in loose['violations'])
    assert any(v['type'] == 'center_of_mass' for v in strict['violations'])
    print('[PASS] 门禁重心偏差可配（0.5 稳定 / 0.05 不稳定）')


def test_sanitizer_com_tolerance_same_source():
    """放置层清理(sanitizer)与门禁用同一重心阈值：严格阈值下偏置盘会被清理拆箱。"""
    from src.packing.sanitizer import sanitize_packed_items
    pallet_dims = {'length': 1200, 'width': 1000, 'height': 1450}
    # 两个底面箱：一个压角(重)、一个居中(轻)，整体重心偏向角
    corner = _box('corner', 0, 0, 0, 300, 300, 100, weight=100.0)
    center = _box('center', 450, 350, 0, 300, 300, 100, weight=1.0)
    items = [corner, center]

    kept_loose, rm_loose = sanitize_packed_items(
        items, pallet_dims=pallet_dims, center_of_mass_tolerance=0.5
    )
    kept_strict, rm_strict = sanitize_packed_items(
        items, pallet_dims=pallet_dims, center_of_mass_tolerance=0.02
    )
    # 宽松阈值：重心在容差内，不拆箱
    assert len(kept_loose) == 2 and not rm_loose
    # 严格阈值：重心超界 → sanitizer 拆掉箱子（与门禁同源，不会出现门禁拒但放置放行）
    assert len(kept_strict) < 2
    print('[PASS] sanitizer 重心阈值与门禁同源（0.5 不拆 / 0.02 拆箱）')


if __name__ == '__main__':
    test_defaults()
    test_from_dict_tolerant()
    test_frozen()
    test_loader_robot_alias()
    test_gate_small_box_switch()
    test_gate_gap_value_configurable()
    test_gate_com_tolerance_configurable()
    test_sanitizer_com_tolerance_same_source()
    print('\n所有约束配置测试通过！')
