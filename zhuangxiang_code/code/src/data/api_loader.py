"""
HTTP API 数据加载器

从 WCS 接口获取库存数据，转换为与 excel_loader.load_boxes 相同结构的箱子列表。
接口只提供库存快照；BMS（min_pack_multiple）和托盘尺寸仍从本地参考 Excel 读取。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests
import urllib3

from src.config.constants import SMALL_BOX_BMS_SHEET, SMALL_BOX_SOURCE_SHEET
from .excel_loader import _detect_small_box_threshold

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# TODO: Mock 库存接口（Postman Mock Server），完整地址见 stock_api_url()
#       POST {base}/adaptor/api/wcs/reqstockinfo
#       默认 base: https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io
#       也可通过环境变量 WCS_MOCK_URL 或 packing_config.yaml → data_source.api_base_url 覆盖
DEFAULT_BASE_URL = os.getenv(
    "WCS_MOCK_URL",
    "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
)

_STOCK_API_PATH = "/adaptor/api/wcs/reqstockinfo"

_BMS_DF = pd.DataFrame()
_PALLET_DIMS_MAP: Dict[str, Dict[str, float]] = {}
_REFERENCE_FILE: Optional[Path] = None


def configure_reference_excel(reference_file: Path) -> None:
    """加载本地 BMS / 托盘尺寸参考表（接口模式专用，非用户订单 Excel）。"""
    global _BMS_DF, _PALLET_DIMS_MAP, _REFERENCE_FILE
    reference_file = Path(reference_file).resolve()
    _REFERENCE_FILE = reference_file
    _BMS_DF = pd.DataFrame()
    _PALLET_DIMS_MAP = {}

    if not reference_file.exists():
        print(f"警告：BMS 参考文件不存在：{reference_file}")
        return

    try:
        _BMS_DF = pd.read_excel(reference_file, sheet_name=SMALL_BOX_BMS_SHEET)
        _BMS_DF = _BMS_DF.set_index("包装规格代码")
    except Exception as exc:
        print(f"警告：读取 BMS 表失败，min_pack_multiple 将使用默认值 0。错误: {exc}")

    try:
        excel = pd.ExcelFile(reference_file)
        source_sheet = SMALL_BOX_SOURCE_SHEET
        if source_sheet not in excel.sheet_names:
            for sheet_name in excel.sheet_names:
                if sheet_name not in {SMALL_BOX_BMS_SHEET, "说明"}:
                    source_sheet = sheet_name
                    break
        df_tasks = pd.read_excel(reference_file, sheet_name=source_sheet)
        for _, row in df_tasks.drop_duplicates(subset=["Case类型"]).iterrows():
            case_type = str(row["Case类型"])
            _PALLET_DIMS_MAP[case_type] = {
                "length": float(row.get("托盘长", 0) or 0),
                "width": float(row.get("托盘宽", 0) or 0),
                "height": float(row.get("托盘高", 0) or 0),
            }
        print(f"[API] 已从参考 Excel 加载托盘尺寸映射：{_PALLET_DIMS_MAP}")
    except Exception as exc:
        print(f"警告：读取参考 Excel 托盘尺寸失败: {exc}")


def stock_api_url(base_url: Optional[str] = None) -> str:
    """返回库存接口完整 URL（便于日志与调试）。"""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}{_STOCK_API_PATH}"


def _print_stock_payload_summary(body: dict, source_label: str) -> None:
    """把接口返回的库存数据摘要打印到终端（开发调试用）。"""
    code = body.get("code")
    msg = body.get("msg", "")
    entries = body.get("data") or []
    if not entries:
        print(f"[接口数据] 来源: {source_label}")
        print("[接口数据] data 为空，无库存条目。")
        return

    total_boxes = sum(int(e.get("target_num", 0) or 0) for e in entries)
    orders = sorted({str(e.get("order_id", "")) for e in entries if e.get("order_id")})
    print(f"[接口数据] 来源: {source_label}")
    print(
        f"[接口数据] 接口返回 {len(entries)} 条记录（每种箱型/订单组合一行），"
        f"不是 {len(entries)} 个箱子"
    )
    print(f"[接口数据] code={code}, msg={msg}")
    print(
        f"[接口数据] 待装总箱数 = 所有 target_num 之和 = {total_boxes} "
        f"（例如 target_num=110 表示该箱型要装 110 个），涉及订单数={len(orders)}"
    )
    if orders:
        preview_orders = ", ".join(orders[:5])
        if len(orders) > 5:
            preview_orders += f" ... 等 {len(orders)} 个"
        print(f"[接口数据] 订单号示例: {preview_orders}")

    print("[接口数据] 箱型明细（box_type | case_type | target_num | order_id | L×W×H | weight）:")
    show_max = 20
    for idx, entry in enumerate(entries[:show_max]):
        box_type = entry.get("box_type", "?")
        case_type = entry.get("case_type", "?")
        target_num = int(entry.get("target_num", 0) or 0)
        order_id = entry.get("order_id", "?")
        length = entry.get("length", "?")
        width = entry.get("width", "?")
        height = entry.get("height", "?")
        weight = entry.get("weight", "?")
        print(
            f"  [{idx + 1:02d}] {box_type} | {case_type} | x{target_num} | "
            f"{order_id} | {length}×{width}×{height} | {weight}"
        )
    if len(entries) > show_max:
        print(f"  ... 其余 {len(entries) - show_max} 条省略，完整内容见 input/ 下保存的 JSON")

    try:
        preview_json = json.dumps(entries[:3], ensure_ascii=False, indent=2)
        print(f"[接口数据] 原始 data 前 3 条 JSON 预览:\n{preview_json}")
    except Exception:
        pass


def _make_msg_header() -> Dict[str, str]:
    return {
        "msgtime": time.strftime("%Y年%m月%d日%H:%M:%S"),
        "msgid": uuid.uuid4().hex,
    }


def _fetch_stock(base_url: str) -> List[Dict]:
    url = stock_api_url(base_url)
    print(f"[下载] 请求接口: POST {url}")
    resp = requests.post(url, json=_make_msg_header(), timeout=30, verify=False)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"接口返回错误: code={body.get('code')}, msg={body.get('msg')}"
        )
    _print_stock_payload_summary(body, source_label=url)
    return body.get("data", [])


def _get_pallet_dims(case_type: str) -> Dict[str, float]:
    dims = _PALLET_DIMS_MAP.get(case_type, {})
    if not dims:
        print(f"  警告：参考 Excel 中未找到 case_type={case_type} 的托盘尺寸。")
    return dims


def _expand_stock_to_boxes(
    stock_entries: List[Dict],
    pallet_dims_map: Dict[str, Dict[str, float]],
) -> List[Dict]:
    boxes: List[Dict] = []
    for entry in stock_entries:
        box_type = entry.get("box_type", "UNKNOWN")
        case_type = entry.get("case_type", "MH423C")
        order_id = entry.get("order_id", "UNKNOWN_ORDER")
        target_num = int(entry.get("target_num", 0) or 0)

        length = float(entry.get("length", 0) or 0)
        width = float(entry.get("width", 0) or 0)
        height = float(entry.get("height", 0) or 0)
        weight = float(entry.get("weight", 0) or 0)
        dims = pallet_dims_map.get(case_type, {})

        if not _BMS_DF.empty and box_type in _BMS_DF.index:
            min_pack_multiple = float(_BMS_DF.loc[box_type, "最小包装量的倍数"])
        else:
            min_pack_multiple = 0.0

        for i in range(target_num):
            box_id = f"{order_id}_{box_type}-{i + 1}"
            boxes.append(
                {
                    "id": box_id,
                    "original_box_id": box_id,
                    "type": box_type,
                    "length": length,
                    "width": width,
                    "height": height,
                    "weight": weight,
                    "min_pack_multiple": min_pack_multiple,
                    "pallet_type": case_type,
                    "sales_order_no": str(order_id),
                    "pallet_dims": dict(dims),
                    "is_small_box": False,
                    "volume": length * width * height,
                    "包装规格代码": str(box_type),
                    "product_code": entry.get("product_code"),
                    "case_group": entry.get("case_group", 0),
                }
            )
    return boxes


def _apply_small_box_flags(all_boxes: List[Dict]) -> List[Dict]:
    if not all_boxes:
        return all_boxes

    df_boxes = pd.DataFrame(all_boxes)
    df_boxes["体积(mm^3)"] = df_boxes["length"] * df_boxes["width"] * df_boxes["height"]
    df_boxes["体积(m^3)"] = df_boxes["体积(mm^3)"] / 1_000_000_000.0
    df_boxes["密度(kg/m^3)"] = df_boxes["weight"] / df_boxes["体积(m^3)"]
    df_boxes["密度/体积指数"] = df_boxes["密度(kg/m^3)"] / df_boxes["体积(m^3)"]

    threshold_volume = _detect_small_box_threshold(
        df_boxes[["包装规格代码", "体积(mm^3)", "密度/体积指数"]]
    )
    if threshold_volume is None:
        threshold_volume = float("inf")
        df_boxes["is_small_box"] = False
    else:
        df_boxes["is_small_box"] = df_boxes["体积(mm^3)"] < threshold_volume - 1e-9

    small_box_count = int(df_boxes["is_small_box"].sum())
    non_small_box_count = int((~df_boxes["is_small_box"]).sum())
    threshold_text = (
        "未能检测到有效阈值"
        if not np.isfinite(threshold_volume)
        else f"{threshold_volume:.2f} mm^3"
    )
    print(f"  小箱子阈值: {threshold_text}，小箱: {small_box_count}，非小箱: {non_small_box_count}")

    records = df_boxes.drop(
        columns=["体积(mm^3)", "体积(m^3)", "密度(kg/m^3)", "密度/体积指数"],
        errors="ignore",
    ).to_dict("records")
    for box in records:
        box.setdefault("is_small_box", False)
        box.setdefault("volume", box["length"] * box["width"] * box["height"])
        box.setdefault("weight", float(box.get("weight", 0) or 0))
    return records


def _boxes_from_stock_entries(stock_entries: List[Dict]) -> Optional[List[Dict]]:
    case_types = {entry.get("case_type", "MH423C") for entry in stock_entries}
    pallet_dims_map = {ct: _get_pallet_dims(ct) for ct in case_types}
    all_boxes = _expand_stock_to_boxes(stock_entries, pallet_dims_map)
    print(
        f"  共展开为 {len(all_boxes)} 个个体箱子（由 {len(stock_entries)} 条接口记录按 target_num 展开）。"
    )
    all_boxes = _apply_small_box_flags(all_boxes)
    return all_boxes or None


def fetch_and_save_stock_json(
    input_dir: Path,
    base_url: Optional[str] = None,
) -> Optional[Path]:
    """请求接口一次，把原始 JSON 保存到 input_dir。"""
    if base_url is None:
        base_url = DEFAULT_BASE_URL
    try:
        url = stock_api_url(base_url)
        print(f"[下载] 请求接口: POST {url}")
        resp = requests.post(url, json=_make_msg_header(), timeout=30, verify=False)
        resp.raise_for_status()
        body = resp.json()
        input_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = input_dir / f"{ts}.json"
        filepath.write_text(resp.text, encoding="utf-8")
        print(f"[下载] {ts} → 已保存 {filepath.resolve()}")
        _print_stock_payload_summary(body, source_label=str(filepath.name))
        return filepath
    except Exception as exc:
        print(f"[下载] 错误: {exc}")
        return None


def load_boxes_from_local_json(filepath: str) -> Optional[List[Dict]]:
    """从本地已保存的库存 JSON 加载箱子列表。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            body = json.load(f)
        if body.get("code") != 0:
            print(
                f"[加载] JSON 内容错误: code={body.get('code')}, msg={body.get('msg')}"
            )
            return None
        stock_entries = body.get("data", [])
        print(
            f"  从文件 {Path(filepath).name} 读取到 {len(stock_entries)} 种箱型。"
        )
        _print_stock_payload_summary(body, source_label=Path(filepath).name)
        return _boxes_from_stock_entries(stock_entries)
    except Exception as exc:
        print(f"[加载] 读取文件 {filepath} 时发生异常: {exc}")
        return None
