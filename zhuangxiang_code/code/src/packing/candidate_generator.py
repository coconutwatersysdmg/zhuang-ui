"""
候选点生成器

负责生成装箱候选放置点。
"""

from typing import Dict, List


class CandidatePointGenerator:
    """
    候选点生成器

    基于已放置箱子的角点生成候选放置点，用于贪心算法寻找下一个箱子的放置位置。
    """

    def __init__(self, max_candidate_points: int = 200, max_points_per_layer: int = 40):
        """
        初始化候选点生成器

        Args:
            max_candidate_points: 最大候选点数量
            max_points_per_layer: 每层最大候选点数量
        """
        self.max_candidate_points = max_candidate_points
        self.max_points_per_layer = max_points_per_layer

    def generate_candidate_points(self, placed_boxes: List[Dict]) -> List[Dict[str, float]]:
        """
        生成候选放置点列表

        基于已放置箱子的角点生成候选点，包括原点(0,0,0)和每个已放置箱子的
        三个相邻角点（x+length、y+width、z+height方向）。对候选点进行去重和排序后，
        如果数量超过限制，则按层（z轴）分组并限制每层的点数，以确保搜索效率。

        Args:
            placed_boxes: 已放置箱子的列表，每个元素包含:
                - position (dict): 箱子位置 {'x': float, 'y': float, 'z': float}
                - length (float): 箱子长度（x轴方向）
                - width (float): 箱子宽度（y轴方向）
                - height (float): 箱子高度（z轴方向）

        Returns:
            候选放置点列表，每个元素为 {'x': float, 'y': float, 'z': float}，
            按 (z, y, x) 优先级升序排列，数量不超过 max_candidate_points

        Examples:
            >>> generator = CandidatePointGenerator(max_candidate_points=200)
            >>> placed = [
            ...     {
            ...         'position': {'x': 0, 'y': 0, 'z': 0},
            ...         'length': 100,
            ...         'width': 100,
            ...         'height': 100
            ...     }
            ... ]
            >>> points = generator.generate_candidate_points(placed)
            >>> len(points)
            4
            >>> points[0]
            {'x': 0, 'y': 0, 'z': 0}
        """
        # 添加原点
        points = [{'x': 0, 'y': 0, 'z': 0}]

        # 为每个已放置箱子生成三个相邻角点
        for box in placed_boxes:
            pos = box['position']
            dims = box

            # X方向角点（右侧）
            points.append({
                'x': pos['x'] + dims['length'],
                'y': pos['y'],
                'z': pos['z']
            })

            # Y方向角点（后侧）
            points.append({
                'x': pos['x'],
                'y': pos['y'] + dims['width'],
                'z': pos['z']
            })

            # Z方向角点（上方）
            points.append({
                'x': pos['x'],
                'y': pos['y'],
                'z': pos['z'] + dims['height']
            })

        # 去重：将字典转换为元组集合，再转回字典
        unique_points = [dict(t) for t in {tuple(d.items()) for d in points}]

        # 排序：按 (z, y, x) 优先级升序
        unique_points.sort(key=lambda p: (p['z'], p['y'], p['x']))

        # 如果候选点数量在限制内，直接返回
        if len(unique_points) <= self.max_candidate_points:
            return unique_points

        # 按Z轴分层
        grouped_points = {}
        for point in unique_points:
            z = point['z']
            if z not in grouped_points:
                grouped_points[z] = []
            grouped_points[z].append(point)

        # 限制每层的点数
        limited_points = []
        for z in sorted(grouped_points.keys()):
            layer_points = grouped_points[z]

            # 如果该层点数超过限制，只保留前max_points_per_layer个
            if len(layer_points) > self.max_points_per_layer:
                layer_points = layer_points[:self.max_points_per_layer]

            limited_points.extend(layer_points)

            # 如果总点数已达到限制，停止添加
            if len(limited_points) >= self.max_candidate_points:
                break

        return limited_points[:self.max_candidate_points]
