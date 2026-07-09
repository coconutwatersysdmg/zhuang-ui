"""
最终装箱方案输出格式化

把装箱算法内部使用的 packed_items（含 raw_length 等冗余字段）转为对外
JSON 报告所需的精简形态：恢复原始尺寸、重算 supported_area、
重算 suction_rect、刷新 stability_checks。

提取自原 zhuangxiang._make_json_output_plan + _recalculate_suction_rect_for_output。
"""

from copy import deepcopy
from typing import Dict, List

from src.geometry.center_of_mass import refresh_pallet_stability_status
from src.geometry.support import calculate_direct_supported_area


def _recalculate_suction_rect(item: Dict) -> None:
    """根据 box 角点 + cup 尺寸重算 suction_rect。"""
    corner = item.get('suction_box_corner')
    cup_x_size = item.get('suction_cup_x_size')
    cup_y_size = item.get('suction_cup_y_size')
    point = item.get('position')
    if not corner or cup_x_size is None or cup_y_size is None or not point:
        return

    cup_x_size = float(cup_x_size)
    cup_y_size = float(cup_y_size)
    x_min = float(point['x'])
    x_max = x_min + float(item.get('length', 0) or 0)
    y_min = float(point['y'])
    y_max = y_min + float(item.get('width', 0) or 0)
    corner_specs = {
        'x_min_y_min': (x_min, y_min, 1, 1),
        'x_max_y_min': (x_max, y_min, -1, 1),
        'x_min_y_max': (x_min, y_max, 1, -1),
        'x_max_y_max': (x_max, y_max, -1, -1),
    }
    if corner not in corner_specs:
        return

    anchor_x, anchor_y, x_dir, y_dir = corner_specs[corner]
    item['suction_rect_x_min'] = (
        anchor_x if x_dir > 0 else anchor_x - cup_x_size
    )
    item['suction_rect_x_max'] = (
        anchor_x + cup_x_size if x_dir > 0 else anchor_x
    )
    item['suction_rect_y_min'] = (
        anchor_y if y_dir > 0 else anchor_y - cup_y_size
    )
    item['suction_rect_y_max'] = (
        anchor_y + cup_y_size if y_dir > 0 else anchor_y
    )


def build_json_output_plan(
    packing_plan: List[Dict],
    raw_boxes: List[Dict],
    center_of_mass_tolerance: float = 1.0 / 3.0,
) -> List[Dict]:
    """生成对外的 JSON 装箱方案副本。

    - 恢复每个箱子的原始 length/width/height/volume/weight
    - 重算 suction_rect 与 supported_area / support_ratio
    - 刷新 stability_checks（重心偏差阈值 center_of_mass_tolerance，与门禁同源）
    """
    raw_by_id = {str(box.get('id')): box for box in raw_boxes}
    output_plan = deepcopy(packing_plan)

    for pallet in output_plan:
        items = pallet.get('packed_items', [])
        for item in items:
            raw = raw_by_id.get(str(item.get('id')))
            if raw is None:
                continue
            if item.get('layered_oriented'):
                # 列式装箱已按放置朝向写入 raw_*（可能相对原始箱旋转 90°）；
                # 保留它，避免用原始箱尺寸恢复而丢失朝向，破坏间隙/重叠判定。
                raw_length = float(item.get('raw_length', item.get('length', 0)) or 0)
                raw_width = float(item.get('raw_width', item.get('width', 0)) or 0)
                raw_height = float(item.get('raw_height', item.get('height', 0)) or 0)
            else:
                raw_length = float(raw.get('length', item.get('length', 0)) or 0)
                raw_width = float(raw.get('width', item.get('width', 0)) or 0)
                raw_height = float(raw.get('height', item.get('height', 0)) or 0)
            item.setdefault('length', raw_length)
            item.setdefault('width', raw_width)
            item.setdefault('height', raw_height)
            item['raw_length'] = raw_length
            item['raw_width'] = raw_width
            item['raw_height'] = raw_height
            item['original_length'] = raw_length
            item['original_width'] = raw_width
            item['original_height'] = raw_height
            item.pop('layered_oriented', None)  # 内部旋转标记，不进对外输出
            item['volume'] = float(
                raw.get(
                    'volume',
                    raw_length * raw_width * raw_height,
                ) or 0
            )
            if 'weight' in raw:
                item['weight'] = float(raw.get('weight') or 0)
            _recalculate_suction_rect(item)

        for item in items:
            dims = {
                'length': float(item.get('length', 0) or 0),
                'width': float(item.get('width', 0) or 0),
                'height': float(item.get('height', 0) or 0),
            }
            supported_area = calculate_direct_supported_area(
                item['position'], dims, items
            )
            base_area = dims['length'] * dims['width']
            item['supported_area'] = float(supported_area)
            item['support_ratio'] = (
                float(supported_area / base_area) if base_area > 0 else 0.0
            )

        pallet_dims = items[0].get('pallet_dims') if items else None
        if pallet_dims:
            pallet_volume = (
                float(pallet_dims.get('length', 0) or 0)
                * float(pallet_dims.get('width', 0) or 0)
                * float(pallet_dims.get('height', 0) or 0)
            )
            box_total_volume = sum(
                float(
                    item.get(
                        'volume',
                        float(item.get('length', 0) or 0)
                        * float(item.get('width', 0) or 0)
                        * float(item.get('height', 0) or 0),
                    ) or 0
                )
                for item in items
            )
            pallet['box_total_volume'] = round(box_total_volume, 6)
            pallet['pallet_volume'] = round(pallet_volume, 6)
            pallet['fill_rate'] = (
                round(box_total_volume / pallet_volume, 6)
                if pallet_volume > 0 else 0.0
            )
            refresh_pallet_stability_status(
                pallet, pallet_dims, tolerance=center_of_mass_tolerance
            )

    return output_plan
