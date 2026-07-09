"""
放置验证器

负责验证箱子放置的合法性，包括边界检查、重叠检查、稳定性检查等。
"""

from typing import Dict, List

from .stacking_policy import passes_same_size_heavier_below_constraint


class PlacementValidator:
    """
    放置验证器

    验证箱子在指定位置的放置是否合法。
    """

    def __init__(
        self,
        pallet_dims: Dict[str, float],
        support_ratio_threshold: float = 0.8,
        size_tolerance: float = 2.0,
        z_tolerance: float = 0.0
    ):
        """
        初始化放置验证器

        Args:
            pallet_dims: 托盘尺寸 {'length': float, 'width': float, 'height': float}
            support_ratio_threshold: 支撑比例阈值
            size_tolerance: XY方向尺寸容差（毫米）
            z_tolerance: Z方向尺寸容差（毫米）
        """
        self.pallet_dims = pallet_dims
        self.support_ratio_threshold = support_ratio_threshold
        self.size_tolerance = size_tolerance
        self.z_tolerance = z_tolerance

    def can_fit_in_pallet(self, item: Dict) -> bool:
        """
        检查物品是否能够在考虑尺寸公差的情况下放入托盘

        通过比较物品的长宽高与托盘的长宽高（加上尺寸公差），
        判断该物品是否能够被放置到托盘中。

        Args:
            item: 待检查的物品字典，包含以下键：
                - 'length' (float): 物品长度
                - 'width' (float): 物品宽度
                - 'height' (float): 物品高度

        Returns:
            如果物品的长宽高（含公差）均不超过托盘对应维度则返回True，否则返回False

        Examples:
            >>> validator = PlacementValidator(
            ...     pallet_dims={'length': 1200, 'width': 1000, 'height': 1450},
            ...     size_tolerance=2.0
            ... )
            >>> item = {'length': 100, 'width': 100, 'height': 100}
            >>> validator.can_fit_in_pallet(item)
            True
            >>> large_item = {'length': 1300, 'width': 100, 'height': 100}
            >>> validator.can_fit_in_pallet(large_item)
            False
        """
        return (
            item['length'] + self.size_tolerance <= self.pallet_dims['length'] and
            item['width'] + self.size_tolerance <= self.pallet_dims['width'] and
            item['height'] + self.z_tolerance <= self.pallet_dims['height']
        )

    def is_within_bounds(
        self,
        point: Dict[str, float],
        dims: Dict[str, float]
    ) -> bool:
        """
        检查箱子是否在托盘边界内

        Args:
            point: 箱子位置 {'x': float, 'y': float, 'z': float}
            dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}

        Returns:
            如果箱子完全在托盘边界内返回True，否则返回False

        Examples:
            >>> validator = PlacementValidator(
            ...     pallet_dims={'length': 1200, 'width': 1000, 'height': 1450}
            ... )
            >>> point = {'x': 0, 'y': 0, 'z': 0}
            >>> dims = {'length': 100, 'width': 100, 'height': 100}
            >>> validator.is_within_bounds(point, dims)
            True
            >>> point = {'x': 1150, 'y': 0, 'z': 0}
            >>> validator.is_within_bounds(point, dims)
            False
        """
        return (
            point['x'] >= 0 and
            point['y'] >= 0 and
            point['z'] >= 0 and
            point['x'] + dims['length'] <= self.pallet_dims['length'] and
            point['y'] + dims['width'] <= self.pallet_dims['width'] and
            point['z'] + dims['height'] <= self.pallet_dims['height']
        )

    def check_overlap(
        self,
        point: Dict[str, float],
        dims: Dict[str, float],
        placed_boxes: List[Dict]
    ) -> bool:
        """
        检查箱子是否与已放置的箱子重叠

        Args:
            point: 候选位置 {'x': float, 'y': float, 'z': float}
            dims: 候选箱子尺寸 {'length': float, 'width': float, 'height': float}
            placed_boxes: 已放置的箱子列表

        Returns:
            如果有重叠返回True，否则返回False

        Examples:
            >>> validator = PlacementValidator(
            ...     pallet_dims={'length': 1200, 'width': 1000, 'height': 1450}
            ... )
            >>> point = {'x': 50, 'y': 50, 'z': 0}
            >>> dims = {'length': 100, 'width': 100, 'height': 100}
            >>> placed = [{
            ...     'position': {'x': 0, 'y': 0, 'z': 0},
            ...     'length': 100,
            ...     'width': 100,
            ...     'height': 100
            ... }]
            >>> validator.check_overlap(point, dims, placed)
            True
        """
        for box in placed_boxes:
            pos2 = box['position']
            dims2 = box

            # 检查三个轴向的重叠
            overlap_x = (
                point['x'] < pos2['x'] + dims2['length'] and
                point['x'] + dims['length'] > pos2['x']
            )
            overlap_y = (
                point['y'] < pos2['y'] + dims2['width'] and
                point['y'] + dims['width'] > pos2['y']
            )
            overlap_z = (
                point['z'] < pos2['z'] + dims2['height'] and
                point['z'] + dims['height'] > pos2['z']
            )

            if overlap_x and overlap_y and overlap_z:
                return True

        return False

    def is_stable(
        self,
        point: Dict[str, float],
        dims: Dict[str, float],
        placed_boxes: List[Dict]
    ) -> bool:
        """
        检查箱子在指定位置是否稳定

        通过计算支撑比例判断稳定性。地面上的箱子总是稳定的。

        Args:
            point: 箱子位置 {'x': float, 'y': float, 'z': float}
            dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}
            placed_boxes: 已放置的箱子列表

        Returns:
            如果箱子稳定返回True，否则返回False

        Examples:
            >>> validator = PlacementValidator(
            ...     pallet_dims={'length': 1200, 'width': 1000, 'height': 1450},
            ...     support_ratio_threshold=0.8
            ... )
            >>> point = {'x': 0, 'y': 0, 'z': 0}
            >>> dims = {'length': 100, 'width': 100, 'height': 100}
            >>> validator.is_stable(point, dims, [])
            True
        """
        # 地面上的箱子总是稳定的
        if point['z'] == 0:
            return True

        # 计算支撑比例
        from ..geometry.support import direct_support_ratio
        support_ratio = direct_support_ratio(point, dims, placed_boxes)

        return support_ratio >= self.support_ratio_threshold

    def satisfies_stacking_order(
        self,
        item: Dict,
        point: Dict[str, float],
        dims: Dict[str, float],
        placed_boxes: List[Dict],
    ) -> bool:
        """Check same-size heavier-below stacking order."""
        return passes_same_size_heavier_below_constraint(
            item,
            point,
            dims,
            placed_boxes,
        )

    def satisfies_small_box_support_order(
        self,
        item: Dict,
        point: Dict[str, float],
        dims: Dict[str, float],
        placed_boxes: List[Dict],
    ) -> bool:
        """小箱不压大箱：小箱不得直接置于体积更大的箱子之上。"""
        from ..utils.helpers import passes_small_box_not_on_larger_constraint
        return passes_small_box_not_on_larger_constraint(
            item, point, dims, placed_boxes
        )
