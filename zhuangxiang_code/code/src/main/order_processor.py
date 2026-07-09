"""
订单处理器

负责数据预处理和按 (托盘类型, 销售订单号) 对箱子分组。
"""

from typing import Callable, Dict, List, Optional, Tuple


class OrderProcessor:
    """订单处理器：加载箱子数据并按托盘类型与销售订单号分组。"""

    def __init__(self, preprocess_fn: Callable[..., List[Dict]]):
        """
        Args:
            preprocess_fn: 数据预处理函数，返回箱子列表。
        """
        self._preprocess_fn = preprocess_fn

    def load_boxes(self, filepath: Optional[str] = None) -> List[Dict]:
        """加载并预处理所有箱子数据。"""
        boxes = self._preprocess_fn(filepath) if filepath else self._preprocess_fn()
        return boxes or []

    @staticmethod
    def group_by_order(
        boxes: List[Dict]
    ) -> Dict[Tuple[str, str], List[Dict]]:
        """
        按 (托盘类型, 销售订单号) 分组箱子。

        Args:
            boxes: 箱子列表，每个箱子需包含 'pallet_type' 和 'sales_order_no'。

        Returns:
            分组字典，键为 (pallet_type, sales_order_no)。
        """
        grouped: Dict[Tuple[str, str], List[Dict]] = {}
        for box in boxes:
            key = (
                box['pallet_type'],
                box.get('sales_order_no', 'UNKNOWN_ORDER')
            )
            grouped.setdefault(key, []).append(box)
        return grouped

    def prepare(
        self, filepath: Optional[str] = None
    ) -> Tuple[List[Dict], Dict[Tuple[str, str], List[Dict]]]:
        """一步完成加载+分组。"""
        boxes = self.load_boxes(filepath)
        return boxes, self.group_by_order(boxes)
