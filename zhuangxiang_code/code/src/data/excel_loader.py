"""
Excel 数据加载与箱子预处理

把原 zhuangxiang.preprocess_data 拆出来，移除对全局常量的隐式依赖。
"""

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.config.constants import (
    SMALL_BOX_BMS_SHEET,
    SMALL_BOX_INDEX_MIN_SLOPE_WINDOW,
    SMALL_BOX_INDEX_NEAR_PEAK_GAP,
    SMALL_BOX_INDEX_PLATEAU_ABS_TOL,
    SMALL_BOX_INDEX_PLATEAU_REL_TOL,
    SMALL_BOX_INDEX_PLATEAU_WINDOW,
    SMALL_BOX_INDEX_SMOOTH_WINDOW,
    SMALL_BOX_SOURCE_FILE,
    SMALL_BOX_SOURCE_SHEET,
)


def _detect_small_box_threshold(df_boxes: pd.DataFrame) -> Optional[float]:
    """按体积与密度/体积指数曲线检测小箱子体积阈值。"""
    if df_boxes is None or df_boxes.empty:
        return None

    df = df_boxes.copy()
    df = df.dropna(subset=['体积(mm^3)', '密度/体积指数'])
    if df.empty:
        return None

    df = df.sort_values(by=['体积(mm^3)', '包装规格代码']).reset_index(drop=True)
    smooth_window = min(SMALL_BOX_INDEX_SMOOTH_WINDOW, len(df))
    df['平滑指数'] = df['密度/体积指数'].rolling(
        window=smooth_window, center=True, min_periods=1
    ).mean()

    smoothed = df['平滑指数'].to_numpy(dtype=float)
    volumes = df['体积(mm^3)'].to_numpy(dtype=float)

    if len(df) == 1:
        return float(volumes[0])

    peak_idx = int(np.argmax(smoothed))
    peak_value = float(smoothed[peak_idx])
    near_peak_floor = peak_value * (1.0 - SMALL_BOX_INDEX_NEAR_PEAK_GAP)
    slope = np.diff(smoothed)
    slope_scale = max(float(np.max(np.abs(slope))) if len(slope) else 0.0, 1.0)
    plateau_window = min(SMALL_BOX_INDEX_PLATEAU_WINDOW, len(df))
    slope_window = min(SMALL_BOX_INDEX_MIN_SLOPE_WINDOW, max(1, len(df) - 1))

    plateau_start_idx = None
    for i in range(peak_idx, len(df) - plateau_window + 1):
        window_vals = smoothed[i:i + plateau_window]
        if len(window_vals) < plateau_window:
            break
        window_delta = float(window_vals[-1] - window_vals[0])
        window_range = float(np.max(window_vals) - np.min(window_vals))
        window_std = float(np.std(window_vals))

        prev_segment = smoothed[max(0, i - slope_window):i]
        prev_slope = (
            float(np.mean(np.diff(prev_segment))) if len(prev_segment) >= 2 else 0.0
        )

        rel_tol = max(
            peak_value * SMALL_BOX_INDEX_PLATEAU_REL_TOL,
            SMALL_BOX_INDEX_PLATEAU_ABS_TOL,
        )
        is_flat = (
            abs(window_delta) <= rel_tol
            and window_range <= rel_tol * 1.5
            and window_std <= max(
                peak_value * SMALL_BOX_INDEX_PLATEAU_REL_TOL,
                SMALL_BOX_INDEX_PLATEAU_ABS_TOL * 0.75,
            )
        )
        is_not_rising = window_delta <= slope_scale * 0.15
        prev_trend_slow = prev_slope <= slope_scale * 0.2
        if (
            smoothed[i] >= near_peak_floor
            and is_flat
            and is_not_rising
            and prev_trend_slow
        ):
            plateau_start_idx = i
            break

    if plateau_start_idx is None:
        for i in range(peak_idx, len(df)):
            if smoothed[i] <= near_peak_floor:
                plateau_start_idx = i
                break

    if plateau_start_idx is None:
        plateau_start_idx = peak_idx

    return float(volumes[plateau_start_idx])


def load_boxes(filepath: Optional[str] = None) -> Optional[List[Dict]]:
    """加载箱子数据并预处理。

    Args:
        filepath: Excel 文件路径；None 时使用 SMALL_BOX_SOURCE_FILE 默认值。

    Returns:
        箱子字典列表；文件不存在时返回 None。
    """
    if filepath is None:
        filepath = SMALL_BOX_SOURCE_FILE
    else:
        filepath = Path(filepath)

    try:
        excel = pd.ExcelFile(filepath)
        source_sheet = SMALL_BOX_SOURCE_SHEET
        if source_sheet not in excel.sheet_names:
            for sheet_name in excel.sheet_names:
                if sheet_name not in {SMALL_BOX_BMS_SHEET, "说明"}:
                    source_sheet = sheet_name
                    break
        df_tasks = pd.read_excel(filepath, sheet_name=source_sheet)
        df_bms = pd.read_excel(filepath, sheet_name=SMALL_BOX_BMS_SHEET)
    except FileNotFoundError:
        print(f"错误：未找到文件 '{filepath}'。")
        return None

    df_bms = df_bms.set_index('包装规格代码')
    raw_ids = df_tasks['箱子序号'].astype(str)
    duplicated_raw_ids = set(raw_ids[raw_ids.duplicated(keep=False)])
    all_boxes = []
    for row_idx, row in df_tasks.iterrows():
        original_box_id = row['箱子序号']
        box_id = (
            f"{original_box_id}__row{row_idx}"
            if str(original_box_id) in duplicated_raw_ids
            else original_box_id
        )
        box_type = row['Box类型']
        raw_weight = row.get('总重量', 0)
        weight = pd.to_numeric(raw_weight, errors='coerce')
        if pd.isna(weight):
            weight = 0.0
        min_pack_multiple = (
            df_bms.loc[box_type, '最小包装量的倍数']
            if box_type in df_bms.index else 0
        )
        sales_order_no = row.get('销售订单号', 'UNKNOWN_ORDER')
        if pd.isna(sales_order_no) or str(sales_order_no).strip() == "":
            sales_order_no = 'UNKNOWN_ORDER'
        length = pd.to_numeric(row['箱子长'], errors='coerce')
        width = pd.to_numeric(row['箱子宽'], errors='coerce')
        height = pd.to_numeric(row['箱子高'], errors='coerce')
        all_boxes.append({
            "id": box_id,
            "original_box_id": original_box_id,
            "type": box_type,
            "length": float(length) if pd.notna(length) else 0.0,
            "width": float(width) if pd.notna(width) else 0.0,
            "height": float(height) if pd.notna(height) else 0.0,
            "weight": float(weight),
            "min_pack_multiple": float(min_pack_multiple),
            "pallet_type": row['Case类型'],
            "sales_order_no": str(sales_order_no),
            "pallet_dims": {
                "length": row['托盘长'],
                "width": row['托盘宽'],
                "height": row['托盘高'],
            },
        })

    df_boxes = pd.DataFrame(all_boxes)
    df_boxes['包装规格代码'] = df_boxes['type'].astype(str)
    df_boxes['体积(mm^3)'] = (
        df_boxes['length'] * df_boxes['width'] * df_boxes['height']
    )
    df_boxes['体积(m^3)'] = df_boxes['体积(mm^3)'] / 1_000_000_000.0
    df_boxes['密度(kg/m^3)'] = df_boxes['weight'] / df_boxes['体积(m^3)']
    df_boxes['密度/体积指数'] = df_boxes['密度(kg/m^3)'] / df_boxes['体积(m^3)']

    threshold_volume = _detect_small_box_threshold(
        df_boxes[['包装规格代码', '体积(mm^3)', '密度/体积指数']]
    )
    if threshold_volume is None:
        threshold_volume = float('inf')
        df_boxes['is_small_box'] = False
    else:
        df_boxes['is_small_box'] = df_boxes['体积(mm^3)'] < threshold_volume - 1e-9

    small_box_count = int(df_boxes['is_small_box'].sum())
    non_small_box_count = int((~df_boxes['is_small_box']).sum())
    threshold_text = (
        '未能检测到有效阈值' if not np.isfinite(threshold_volume)
        else f'{threshold_volume:.2f} mm^3'
    )
    print(f"检测到小箱子体积阈值: {threshold_text}")
    print(f"小箱子数量: {small_box_count}，非小箱子数量: {non_small_box_count}")

    all_boxes = df_boxes.drop(
        columns=['体积(mm^3)', '体积(m^3)', '密度(kg/m^3)', '密度/体积指数'],
        errors='ignore',
    ).to_dict('records')
    for box in all_boxes:
        box.setdefault('is_small_box', False)
        box.setdefault('volume', box['length'] * box['width'] * box['height'])
        box.setdefault('weight', float(box.get('weight', 0) or 0))
    return all_boxes
