"""
全局常量定义

从原 zhuangxiang.py 提取的硬编码常量。
"""

from pathlib import Path

# ============================================================================
# 项目路径配置
# ============================================================================
# 项目根目录：本文件位于 code2/src/config/，向上 4 级 = zhuangxiang_code/
# 与原 zhuangxiang.py 保持一致（zhuangxiang.py 在 code2/，parent.parent = zhuangxiang_code/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# ============================================================================
# 托盘类型和要求的指数
# ============================================================================
PALLET_INDEX_TARGETS = {
    "MH423C": 192,
    "MH110": 32
}

# ============================================================================
# 装箱约束常量
# ============================================================================
# 箱间最大间隙（毫米）
MAX_BOX_GAP_MM = 6.0

# 全局保守开关：保留为 False，但在"近/中等缺口"明显时可选择性放行
ENABLE_EXPENSIVE_FAILED_REPACK = False


# ============================================================================
# Excel 数据源（preprocess 用）
# ============================================================================
#SMALL_BOX_SOURCE_FILE = DATA_DIR / "多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx"
SMALL_BOX_SOURCE_FILE = DATA_DIR / "668箱子数据集.xlsx"
SMALL_BOX_SOURCE_SHEET = "最终挑选结果"
SMALL_BOX_BMS_SHEET = "包装物料主数据(BMS)"

# 小箱子体积阈值检测参数（基于密度/体积指数曲线）
SMALL_BOX_INDEX_SMOOTH_WINDOW = 5
SMALL_BOX_INDEX_PLATEAU_WINDOW = 6
SMALL_BOX_INDEX_PLATEAU_REL_TOL = 0.02
SMALL_BOX_INDEX_PLATEAU_ABS_TOL = 0.8
SMALL_BOX_INDEX_MIN_SLOPE_WINDOW = 3
SMALL_BOX_INDEX_NEAR_PEAK_GAP = 0.15
