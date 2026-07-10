"""
数据加载模块

加载 Excel / API / 本地 JSON 数据并预处理为装箱算法可用的字典列表。
"""

from .excel_loader import load_boxes
from .api_loader import (
    configure_reference_excel,
    fetch_and_save_stock_json,
    load_boxes_from_local_json,
)

__all__ = [
    "load_boxes",
    "configure_reference_excel",
    "fetch_and_save_stock_json",
    "load_boxes_from_local_json",
]
