"""
订单处理器

负责数据预处理和按 (托盘类型, 销售订单号) 对箱子分组。
带非 0 ``case_group`` 的箱子在订单号键上追加内部后缀细分成独立子组
（同 case_group 才能同托盘），输出前由 workflow 剥离还原。
"""

from typing import Callable, Dict, List, Optional, Tuple

from ..utils.case_group import tag_sales_order_no


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

        箱子可带可选字段 ``case_group``（0/缺失＝无约束）：非 0 值时该箱只能与
        相同 case_group 的箱子同托盘——实现为在订单号键上追加内部后缀，使不同
        case_group 落入不同分组（组间永不混盘，覆盖主装箱与全部救援路径）；
        后缀由 workflow 在输出前剥离还原，对外不可见。case_group 全为 0/缺失时
        分组键与历史完全一致（零行为变化）。

        Args:
            boxes: 箱子列表，每个箱子需包含 'pallet_type' 和 'sales_order_no'，
                可选 'case_group'。

        Returns:
            分组字典，键为 (pallet_type, sales_order_no[+内部case_group后缀])。
        """
        grouped: Dict[Tuple[str, str], List[Dict]] = {}
        for box in boxes:
            key = (
                box['pallet_type'],
                tag_sales_order_no(
                    box.get('sales_order_no', 'UNKNOWN_ORDER'),
                    box.get('case_group'),
                ),
            )
            grouped.setdefault(key, []).append(box)
        return grouped

    def prepare(
        self, filepath: Optional[str] = None
    ) -> Tuple[List[Dict], Dict[Tuple[str, str], List[Dict]]]:
        """一步完成加载+分组。"""
        boxes = self.load_boxes(filepath)
        return boxes, self.group_by_order(boxes)
