"""单盘增量约束门禁（top-up 试放的等价加速）。

前提：placed 已满足 validate_pallet_constraints 的几何部分。在其上新增
一个 new_box 时，只需校验：

1. new_box 自身：越界 / 吸盘字段 / 与既有箱三轴重叠 / 支撑率 / 间隙；
2. 与 new_box 在 Z 区间重叠的既有箱的间隙（新箱可能成为其更近邻居，
   也可能让"该方向原本无邻居"的箱子出现 >=6mm 的违例间隙）。

其余项不会因新增 new_box 而新违例：既有箱的越界/吸盘字段不变、
两两重叠不变、支撑面积只增不减（新箱顶面齐平时只会增加支撑者）。

复用与 validate_pallet_constraints 完全相同的底层原语与容差，
判定行为等价。重心（center_of_mass）不在此处判定，由调用方单独的
重心校验负责（与原 top-up 流程一致）。
"""

from typing import Dict, List

from ..geometry.constraint_validator import REQUIRED_SUCTION_FIELDS
from ..geometry.gap_checker import passes_box_gap_constraint
from ..geometry.overlap import axis_overlap_len
from ..geometry.support import direct_support_ratio


def _dims(item: Dict) -> Dict[str, float]:
    return {
        'length': float(item.get('length', 0) or 0),
        'width': float(item.get('width', 0) or 0),
        'height': float(item.get('height', 0) or 0),
    }


def _raw(item: Dict) -> Dict[str, float]:
    return {
        'length': float(item.get('raw_length', item.get('length', 0)) or 0),
        'width': float(item.get('raw_width', item.get('width', 0)) or 0),
        'height': float(item.get('raw_height', item.get('height', 0)) or 0),
    }


def incremental_pallet_ok(
    new_box: Dict,
    placed: List[Dict],
    pallet_dims: Dict[str, float],
    support_ratio_threshold: float = 0.8,
    max_gap: float = 6.0,
    require_suction: bool = True,
    constraint_config=None,
) -> bool:
    """在已整盘合法的 placed 上新增 new_box，判定是否仍满足几何硬约束。

    O(N) 增量预检（越界/吸盘字段/重叠/支撑/间隙），是整盘门禁的快速等价
    路径。constraint_config 提供时统一覆盖支撑率/间隙/吸盘开关，与门禁同源；
    不提供时沿用各参数默认值（行为不变）。
    """
    if constraint_config is not None:
        support_ratio_threshold = constraint_config.support_ratio_threshold
        max_gap = constraint_config.max_box_gap_mm
        require_suction = constraint_config.suction_reachability_enabled
    pos = new_box.get('position')
    if not pos:
        return False
    dims = _dims(new_box)
    pallet_length = float(pallet_dims.get('length', 0) or 0)
    pallet_width = float(pallet_dims.get('width', 0) or 0)
    pallet_height = float(pallet_dims.get('height', 0) or 0)

    # 1. new_box 越界
    if (
        pos.get('x', 0) < -1e-9
        or pos.get('y', 0) < -1e-9
        or pos.get('z', 0) < -1e-9
        or pos.get('x', 0) + dims['length'] > pallet_length + 1e-9
        or pos.get('y', 0) + dims['width'] > pallet_width + 1e-9
        or pos.get('z', 0) + dims['height'] > pallet_height + 1e-9
    ):
        return False

    # 2. new_box 吸盘字段齐全
    if require_suction and any(
        new_box.get(field) is None for field in REQUIRED_SUCTION_FIELDS
    ):
        return False

    nx0, nx1 = pos['x'], pos['x'] + dims['length']
    ny0, ny1 = pos['y'], pos['y'] + dims['width']
    nz0, nz1 = pos['z'], pos['z'] + dims['height']

    # 3. new_box 与既有箱三轴重叠
    for other in placed:
        other_pos = other.get('position')
        if not other_pos:
            continue
        other_dims = _dims(other)
        if (
            axis_overlap_len(
                nx0, nx1, other_pos['x'], other_pos['x'] + other_dims['length']
            ) > 1e-9
            and axis_overlap_len(
                ny0, ny1, other_pos['y'], other_pos['y'] + other_dims['width']
            ) > 1e-9
            and axis_overlap_len(
                nz0, nz1, other_pos['z'], other_pos['z'] + other_dims['height']
            ) > 1e-9
        ):
            return False

    # 4. new_box 支撑率
    if pos.get('z', 0) > 1e-9:
        ratio = direct_support_ratio(pos, dims, placed)
        if ratio + 1e-9 < support_ratio_threshold:
            return False

    # 5. new_box 间隙（vs 全部既有箱）
    if not passes_box_gap_constraint(
        pos, dims, _raw(new_box), placed, max_gap=max_gap
    ):
        return False

    # 6. 既有箱间隙：仅与 new_box 有 Z 区间重叠者的判定可能改变
    items_with_new = placed + [new_box]
    for box in placed:
        box_pos = box.get('position')
        if not box_pos:
            continue
        box_dims = _dims(box)
        if axis_overlap_len(
            box_pos['z'], box_pos['z'] + box_dims['height'], nz0, nz1
        ) <= 1e-9:
            continue
        others = [
            other for other in items_with_new
            if other.get('id') != box.get('id') and other.get('position')
        ]
        if not passes_box_gap_constraint(
            box_pos, box_dims, _raw(box), others, max_gap=max_gap
        ):
            return False

    return True
