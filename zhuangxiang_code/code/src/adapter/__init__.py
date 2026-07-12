"""WCS 接口适配层（企业 WCS ↔ 装箱算法的纯数据转换）。"""

from .wcs_adapter import (
    WcsPlanResult,
    build_stock_request,
    default_pallet_dims_map,
    load_bms_map,
    report_to_plan_result,
    stock_to_boxes,
)

__all__ = [
    "WcsPlanResult",
    "build_stock_request",
    "default_pallet_dims_map",
    "load_bms_map",
    "report_to_plan_result",
    "stock_to_boxes",
]
