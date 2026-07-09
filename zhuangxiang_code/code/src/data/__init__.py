"""
数据加载模块

加载 Excel 数据并预处理为装箱算法可用的字典列表。
"""

from .excel_loader import load_boxes

__all__ = ["load_boxes"]
