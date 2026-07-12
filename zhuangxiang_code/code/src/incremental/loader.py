"""Excel adapter for staged order-arrival packing tests."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from src.utils.case_group import normalize_case_group


INITIAL_SHEET = "最终挑选结果"
ADDITION_SHEET = "新增箱"
BMS_SHEET = "包装物料主数据(BMS)"


@dataclass
class IncrementalOrderBatch:
    source_path: Path
    initial_boxes: List[Dict]
    new_boxes: List[Dict]


def load_incremental_excel(path: Path) -> IncrementalOrderBatch:
    """Load initial and newly arrived boxes from a three-sheet Excel file."""
    path = Path(path)
    df_initial = pd.read_excel(path, sheet_name=INITIAL_SHEET)
    df_new = pd.read_excel(path, sheet_name=ADDITION_SHEET)
    df_bms = pd.read_excel(path, sheet_name=BMS_SHEET)
    df_initial = _normalize_initial_sheet(df_initial, df_new)

    mpm_by_type = _build_mpm_index(df_bms)
    initial_boxes = _rows_to_boxes(df_initial, mpm_by_type, id_prefix="initial")
    new_boxes = _rows_to_boxes(df_new, mpm_by_type, id_prefix="new")
    _apply_small_box_flags(initial_boxes + new_boxes)
    return IncrementalOrderBatch(path, initial_boxes, new_boxes)


def _normalize_initial_sheet(
    df_initial: pd.DataFrame,
    df_new: pd.DataFrame,
    expected_initial_count: int = 5000,
) -> pd.DataFrame:
    """Support both true-initial and cumulative first-sheet datasets."""
    if len(df_initial) <= expected_initial_count:
        return df_initial
    if "箱子序号" not in df_initial.columns or "箱子序号" not in df_new.columns:
        return df_initial

    new_ids = set(df_new["箱子序号"].astype(str))
    normalized = df_initial[
        ~df_initial["箱子序号"].astype(str).isin(new_ids)
    ].copy()
    if len(normalized) == expected_initial_count:
        return normalized
    return df_initial


def _build_mpm_index(df_bms: pd.DataFrame) -> Dict[str, float]:
    if "包装规格代码" not in df_bms.columns:
        raise ValueError("BMS sheet missing required column: 包装规格代码")
    if "最小包装量的倍数" not in df_bms.columns:
        raise ValueError("BMS sheet missing required column: 最小包装量的倍数")

    cleaned = df_bms.dropna(subset=["包装规格代码"]).copy()
    cleaned["包装规格代码"] = cleaned["包装规格代码"].astype(str)
    return {
        str(row["包装规格代码"]): float(
            pd.to_numeric(row.get("最小包装量的倍数", 0), errors="coerce") or 0
        )
        for _, row in cleaned.iterrows()
    }


def _rows_to_boxes(
    df: pd.DataFrame,
    mpm_by_type: Dict[str, float],
    id_prefix: str,
) -> List[Dict]:
    required = [
        "箱子序号",
        "销售订单号",
        "Box类型",
        "箱子长",
        "箱子宽",
        "箱子高",
        "Case类型",
        "托盘长",
        "托盘宽",
        "托盘高",
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Order sheet missing required columns: {missing}")

    raw_ids = df["箱子序号"].astype(str)
    duplicated_raw_ids = set(raw_ids[raw_ids.duplicated(keep=False)])
    boxes: List[Dict] = []
    for row_idx, row in df.iterrows():
        raw_id = row["箱子序号"]
        raw_id_text = str(raw_id)
        box_id = raw_id_text
        if raw_id_text in duplicated_raw_ids:
            box_id = f"{raw_id_text}__{id_prefix}_row{row_idx}"

        box_type = str(row["Box类型"])
        length = _num(row.get("箱子长"))
        width = _num(row.get("箱子宽"))
        height = _num(row.get("箱子高"))
        weight = _num(row.get("总重量"))
        sales_order_no = row.get("销售订单号", "UNKNOWN_ORDER")
        if pd.isna(sales_order_no) or str(sales_order_no).strip() == "":
            sales_order_no = "UNKNOWN_ORDER"

        box = {
            "id": box_id,
            "original_box_id": raw_id,
            "type": box_type,
            "length": length,
            "width": width,
            "height": height,
            "weight": weight,
            "min_pack_multiple": float(mpm_by_type.get(box_type, 0.0)),
            "pallet_type": str(row["Case类型"]),
            "sales_order_no": str(sales_order_no),
            # case_group 同组约束（可选列，缺列/空值＝0＝无约束）
            "case_group": normalize_case_group(row.get("case_group", 0)),
            "pallet_dims": {
                "length": _num(row.get("托盘长")),
                "width": _num(row.get("托盘宽")),
                "height": _num(row.get("托盘高")),
            },
        }
        box["volume"] = length * width * height
        boxes.append(box)
    return boxes


def _apply_small_box_flags(boxes: List[Dict]) -> None:
    if not boxes:
        return
    volumes = np.array([float(box.get("volume", 0.0) or 0.0) for box in boxes])
    positive = volumes[volumes > 0]
    if len(positive) == 0:
        threshold = float("inf")
    else:
        threshold = float(np.quantile(positive, 0.25))
    for box in boxes:
        box["is_small_box"] = float(box.get("volume", 0.0) or 0.0) < threshold


def _num(value) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return 0.0 if pd.isna(number) else float(number)
