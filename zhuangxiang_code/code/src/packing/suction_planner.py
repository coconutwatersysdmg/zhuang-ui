"""
吸盘规划器

负责规划机器人吸盘的放置姿态，确保机器人可达性。
"""

from typing import Dict, List, Optional, Tuple


class SuctionPlanner:
    """
    吸盘规划器

    为箱子规划机器人吸盘的放置姿态，检查机器人可达性。
    """

    def __init__(
        self,
        pallet_dims: Dict[str, float],
        suction_cup_length: float = 600.0,
        suction_cup_width: float = 800.0,
        suction_xy_clearance: float = 0.0,
        suction_z_clearance: float = 0.0,
        allow_suction_rotation_90: bool = True
    ):
        """
        初始化吸盘规划器

        Args:
            pallet_dims: 托盘尺寸 {'length': float, 'width': float, 'height': float}
            suction_cup_length: 吸盘长度（毫米）
            suction_cup_width: 吸盘宽度（毫米）
            suction_xy_clearance: XY方向间隙（毫米）
            suction_z_clearance: Z方向间隙（毫米）
            allow_suction_rotation_90: 是否允许吸盘旋转90度
        """
        self.pallet_dims = pallet_dims
        self.suction_cup_length = float(suction_cup_length)
        self.suction_cup_width = float(suction_cup_width)
        self.suction_xy_clearance = float(suction_xy_clearance)
        self.suction_z_clearance = float(suction_z_clearance)
        self.allow_suction_rotation_90 = allow_suction_rotation_90

    def find_reachable_suction_pose(
        self,
        point: Dict[str, float],
        dims: Dict[str, float],
        placed_boxes: List[Dict],
        raw_dims: Optional[Dict[str, float]] = None,
        preferred_corners: Optional[List[str]] = None
    ) -> Optional[Dict]:
        """
        查找机器人可达的吸盘姿态

        检查分两部分：
        1. 目标箱体footprint竖直下降到目标底面，不能被高于目标底面的箱子占用
        2. 吸盘footprint竖直下降到目标箱顶，不能被高于目标箱顶的箱子占用

        Args:
            point: 箱子位置 {'x': float, 'y': float, 'z': float}
            dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}
            placed_boxes: 已放置的箱子列表
            raw_dims: 箱子原始尺寸（不含容差）
            preferred_corners: 优先选择的角点列表

        Returns:
            可达的吸盘姿态字典，如果所有姿态都被遮挡则返回None
            姿态字典包含：
                - 'box_corner': 箱子角点
                - 'cup_corner': 吸盘角点
                - 'orientation': 吸盘方向
                - 'cup_x_size': 吸盘X方向尺寸
                - 'cup_y_size': 吸盘Y方向尺寸
                - 'cup_rect': 吸盘矩形 {'x_min', 'x_max', 'y_min', 'y_max'}

        Examples:
            >>> planner = SuctionPlanner(
            ...     pallet_dims={'length': 1200, 'width': 1000, 'height': 1450},
            ...     suction_cup_length=600.0,
            ...     suction_cup_width=800.0
            ... )
            >>> point = {'x': 0, 'y': 0, 'z': 0}
            >>> dims = {'length': 100, 'width': 100, 'height': 100}
            >>> pose = planner.find_reachable_suction_pose(point, dims, [])
            >>> pose is not None
            True
        """
        target_rect = self._get_rect_xy(
            point['x'],
            point['y'],
            dims['length'],
            dims['width']
        )
        target_raw_dims = raw_dims if isinstance(raw_dims, dict) else dims
        target_bottom_z = point['z']
        target_top_z = point['z'] + dims['height']

        # 检查目标箱体是否可以竖直下降到目标位置
        if not self._swept_rect_clear_above(
            target_rect,
            placed_boxes,
            clear_z=target_bottom_z,
            z_clearance=0.0
        ):
            return None

        # 枚举所有候选吸盘姿态
        reachable_poses = []
        for pose in self._build_suction_pose_candidates(point, dims):
            # 检查吸盘是否可以竖直下降到目标箱顶
            if self._swept_rect_clear_above(
                pose['cup_rect'],
                placed_boxes,
                clear_z=target_top_z,
                z_clearance=self.suction_z_clearance
            ):
                reachable_poses.append(pose)

        if not reachable_poses:
            return None

        # 选择最优姿态
        reachable_poses.sort(
            key=lambda p: self._suction_pose_sort_key(p, target_rect, preferred_corners)
        )
        return reachable_poses[0]

    def _build_suction_pose_candidates(
        self,
        point: Dict[str, float],
        dims: Dict[str, float]
    ) -> List[Dict]:
        """
        枚举候选位置允许的吸盘姿态

        吸盘一个角点与箱子顶面一个角点对齐，吸盘边方向与箱子x/y方向平行。
        默认枚举4个箱子角点；若允许90度旋转，则同时枚举600x/800y和800x/600y。

        Args:
            point: 箱子位置 {'x': float, 'y': float, 'z': float}
            dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}

        Returns:
            吸盘姿态候选列表
        """
        # 吸盘方向候选
        orientations = [
            (
                self.suction_cup_length,
                self.suction_cup_width,
                f"cup_{self.suction_cup_length:g}x_{self.suction_cup_width:g}y"
            )
        ]

        # 如果允许旋转90度且吸盘不是正方形
        if self.allow_suction_rotation_90 and abs(self.suction_cup_length - self.suction_cup_width) > 1e-9:
            orientations.append(
                (
                    self.suction_cup_width,
                    self.suction_cup_length,
                    f"cup_{self.suction_cup_width:g}x_{self.suction_cup_length:g}y"
                )
            )

        # 箱子四个角点
        x_min = point['x']
        x_max = point['x'] + dims['length']
        y_min = point['y']
        y_max = point['y'] + dims['width']

        corner_specs = [
            ("x_min_y_min", x_min, y_min, 1, 1),    # 左前角
            ("x_max_y_min", x_max, y_min, -1, 1),   # 右前角
            ("x_min_y_max", x_min, y_max, 1, -1),   # 左后角
            ("x_max_y_max", x_max, y_max, -1, -1),  # 右后角
        ]

        poses = []
        for box_corner, anchor_x, anchor_y, x_dir, y_dir in corner_specs:
            for cup_x_size, cup_y_size, orientation in orientations:
                # 计算吸盘矩形
                raw_x_min = anchor_x if x_dir > 0 else anchor_x - cup_x_size
                raw_x_max = anchor_x + cup_x_size if x_dir > 0 else anchor_x
                raw_y_min = anchor_y if y_dir > 0 else anchor_y - cup_y_size
                raw_y_max = anchor_y + cup_y_size if y_dir > 0 else anchor_y

                # 添加间隙
                cup_rect = {
                    'x_min': raw_x_min - self.suction_xy_clearance,
                    'x_max': raw_x_max + self.suction_xy_clearance,
                    'y_min': raw_y_min - self.suction_xy_clearance,
                    'y_max': raw_y_max + self.suction_xy_clearance
                }

                poses.append({
                    'box_corner': box_corner,
                    'cup_corner': box_corner,
                    'orientation': orientation,
                    'cup_x_size': cup_x_size,
                    'cup_y_size': cup_y_size,
                    'cup_rect': cup_rect
                })

        return poses

    def _swept_rect_clear_above(
        self,
        sweep_rect: Dict[str, float],
        placed_boxes: List[Dict],
        clear_z: float,
        z_clearance: float = 0.0
    ) -> bool:
        """
        检查一个XY投影从上方竖直下降到clear_z时是否被已放置箱子遮挡

        若已放箱子与sweep_rect在XY上重叠，且其最高点高于允许高度，
        则该竖直扫掠空间被占用。

        Args:
            sweep_rect: 扫掠矩形 {'x_min', 'x_max', 'y_min', 'y_max'}
            placed_boxes: 已放置的箱子列表
            clear_z: 目标高度
            z_clearance: Z方向间隙

        Returns:
            如果扫掠空间畅通返回True，否则返回False
        """
        max_allowed_z = clear_z - z_clearance

        for placed in placed_boxes:
            placed_pos = placed['position']
            placed_rect = self._get_rect_xy(
                placed_pos['x'],
                placed_pos['y'],
                placed['length'],
                placed['width']
            )

            # 检查XY平面是否重叠
            if not self._rects_overlap_xy(sweep_rect, placed_rect):
                continue

            # 检查已放置箱子的最高点是否超过允许高度
            placed_z_max = placed_pos['z'] + placed['height']
            if placed_z_max > max_allowed_z + 1e-9:
                return False

        return True

    def _suction_pose_sort_key(
        self,
        pose: Dict,
        target_rect: Dict[str, float],
        preferred_corners: Optional[List[str]] = None
    ) -> Tuple:
        """
        为多个可达吸盘姿态提供稳定选择顺序

        优先选择越界更少、吸盘外伸更少、字段顺序更靠前的姿态。

        Args:
            pose: 吸盘姿态
            target_rect: 目标矩形
            preferred_corners: 优先选择的角点列表

        Returns:
            排序键元组
        """
        rect = pose['cup_rect']
        pallet_x_max = self.pallet_dims['length']
        pallet_y_max = self.pallet_dims['width']

        # 计算越界量
        outside_amount = (
            max(0.0, -rect['x_min']) +
            max(0.0, rect['x_max'] - pallet_x_max) +
            max(0.0, -rect['y_min']) +
            max(0.0, rect['y_max'] - pallet_y_max)
        )

        # 计算吸盘外伸面积
        cup_area = (rect['x_max'] - rect['x_min']) * (rect['y_max'] - rect['y_min'])
        target_area = (
            (target_rect['x_max'] - target_rect['x_min']) *
            (target_rect['y_max'] - target_rect['y_min'])
        )
        overhang_area = max(0.0, cup_area - target_area)

        # 计算角点优先级
        if preferred_corners:
            try:
                corner_rank = preferred_corners.index(pose['box_corner'])
            except ValueError:
                corner_rank = len(preferred_corners)
        else:
            corner_rank = 0

        return (
            corner_rank,
            outside_amount,
            overhang_area,
            pose['box_corner'],
            pose['orientation']
        )

    def _get_rect_xy(
        self,
        x: float,
        y: float,
        length: float,
        width: float
    ) -> Dict[str, float]:
        """
        获取XY平面矩形

        Args:
            x: X坐标
            y: Y坐标
            length: 长度
            width: 宽度

        Returns:
            矩形字典 {'x_min', 'x_max', 'y_min', 'y_max'}
        """
        return {
            'x_min': x,
            'x_max': x + length,
            'y_min': y,
            'y_max': y + width
        }

    def _rects_overlap_xy(
        self,
        rect1: Dict[str, float],
        rect2: Dict[str, float]
    ) -> bool:
        """
        检查两个XY平面矩形是否重叠

        Args:
            rect1: 矩形1 {'x_min', 'x_max', 'y_min', 'y_max'}
            rect2: 矩形2 {'x_min', 'x_max', 'y_min', 'y_max'}

        Returns:
            如果重叠返回True，否则返回False
        """
        return (
            rect1['x_min'] < rect2['x_max'] and
            rect1['x_max'] > rect2['x_min'] and
            rect1['y_min'] < rect2['y_max'] and
            rect1['y_max'] > rect2['y_min']
        )
