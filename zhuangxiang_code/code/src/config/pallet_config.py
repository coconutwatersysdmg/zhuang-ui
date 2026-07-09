"""
托盘配置类

定义托盘的物理尺寸和目标指数。
"""

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class PalletConfig:
    """
    托盘配置

    Attributes:
        pallet_type: 托盘类型代码（如 "MH423C", "MH110"）
        length: 托盘长度（毫米）
        width: 托盘宽度（毫米）
        height: 托盘高度（毫米）
        target_index: 目标最小包装量倍数和
    """
    pallet_type: str
    length: float
    width: float
    height: float
    target_index: float

    def to_dims_dict(self) -> Dict[str, float]:
        """
        转换为尺寸字典格式（兼容原代码）

        Returns:
            包含 length, width, height 的字典
        """
        return {
            'length': self.length,
            'width': self.width,
            'height': self.height
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'PalletConfig':
        """
        从字典创建配置对象

        Args:
            data: 包含托盘配置的字典

        Returns:
            PalletConfig 实例
        """
        return cls(
            pallet_type=data['pallet_type'],
            length=float(data['length']),
            width=float(data['width']),
            height=float(data['height']),
            target_index=float(data['target_index'])
        )
