"""Independent orchestration for incremental packing tests."""

import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence

from src.main.result_formatter import ResultFormatter


@dataclass
class IncrementalPackingResult:
    report: Dict
    initial_report: Dict
    incremental_report: Dict
    initial_repack_box_count: int
    new_box_count: int
    total_runtime_seconds: float


def run_incremental_packing(
    initial_boxes: Sequence[Dict],
    new_boxes: Sequence[Dict],
    workflow_factory: Callable[[], object],
    initial_report: Dict = None,
) -> IncrementalPackingResult:
    """Run initial packing, then repack failed pallets with newly arrived boxes."""
    start = time.time()
    initial_boxes = [deepcopy(box) for box in initial_boxes]
    new_boxes = [deepcopy(box) for box in new_boxes]

    if initial_report is None:
        initial_workflow = workflow_factory()
        initial_report = initial_workflow.run_with_boxes(initial_boxes)
    else:
        initial_report = deepcopy(initial_report)
    if initial_report is None:
        initial_report = _empty_report()

    successful_initial_pallets = [
        deepcopy(pallet)
        for pallet in initial_report.get("pallets", [])
        if pallet.get("mpm_status") == "SUCCESS"
    ]
    repack_boxes = _extract_repack_boxes(initial_report)
    incremental_input = repack_boxes + new_boxes

    if incremental_input:
        incremental_workflow = workflow_factory()
        incremental_report = incremental_workflow.run_with_boxes(incremental_input)
        if incremental_report is None:
            incremental_report = _empty_report()
    else:
        incremental_report = _empty_report()

    merged_pallets = successful_initial_pallets + [
        deepcopy(pallet)
        for pallet in incremental_report.get("pallets", [])
        if pallet.get("packed_items")
    ]
    _renumber_pallets(merged_pallets)

    rescue_stats = _build_initial_failed_rescue_stats(
        initial_report, repack_boxes, incremental_report
    )

    expected_boxes = initial_boxes + new_boxes
    ResultFormatter.validate_output_quality(expected_boxes, merged_pallets)
    total_runtime = time.time() - start
    report = _build_merged_report(
        merged_pallets,
        initial_report,
        incremental_report,
        len(repack_boxes),
        len(new_boxes),
        total_runtime,
        rescue_stats,
    )
    return IncrementalPackingResult(
        report=report,
        initial_report=initial_report,
        incremental_report=incremental_report,
        initial_repack_box_count=len(repack_boxes),
        new_box_count=len(new_boxes),
        total_runtime_seconds=round(total_runtime, 2),
    )


def _extract_repack_boxes(report: Dict) -> List[Dict]:
    boxes: List[Dict] = []
    for pallet in report.get("pallets", []):
        if pallet.get("mpm_status") == "SUCCESS":
            continue
        for item in pallet.get("packed_items", []) or []:
            box = deepcopy(item)
            _strip_placement_fields(box)
            boxes.append(box)
    return boxes


def _strip_placement_fields(box: Dict) -> None:
    transient_prefixes = ("suction_",)
    transient_fields = {
        "position",
        "supported_area",
        "support_ratio",
        "raw_length",
        "raw_width",
        "raw_height",
        "original_length",
        "original_width",
        "original_height",
    }
    for key in list(box.keys()):
        if key in transient_fields or key.startswith(transient_prefixes):
            box.pop(key, None)


def _build_initial_failed_rescue_stats(
    initial_report: Dict,
    repack_boxes: Sequence[Dict],
    incremental_report: Dict,
) -> Dict:
    """统计首跑未达标托盘在增量重排后的"救回"情况。

    首跑未达标托盘的箱子会被拆散、与新增箱合并重排，托盘实体不保留。
    因此按以下口径统计：

    - initial_failed_pallet_count: 首跑未达标（非 SUCCESS）托盘数；
    - initial_failed_recovered_pallets: 第二次装箱结果中"达标且至少含
      一个首跑未达标箱子"的托盘数，即救回托盘数；
    - initial_failed_boxes_in_success: 首跑未达标箱子中，最终落在达标
      托盘上的箱数（箱级覆盖，辅助解读）。
    """
    failed_pallet_count = sum(
        1
        for pallet in initial_report.get("pallets", [])
        if pallet.get("mpm_status") != "SUCCESS"
    )
    repack_ids = {box.get("id") for box in repack_boxes}
    recovered_pallets = 0
    boxes_in_success = 0
    for pallet in incremental_report.get("pallets", []):
        if pallet.get("mpm_status") != "SUCCESS":
            continue
        old_in_pallet = sum(
            1
            for item in pallet.get("packed_items", []) or []
            if item.get("id") in repack_ids
        )
        if old_in_pallet:
            recovered_pallets += 1
            boxes_in_success += old_in_pallet
    return {
        "initial_failed_pallet_count": failed_pallet_count,
        "initial_failed_recovered_pallets": recovered_pallets,
        "initial_failed_boxes_in_success": boxes_in_success,
    }


def _renumber_pallets(pallets: List[Dict]) -> None:
    counters: Dict[str, int] = {}
    for pallet in pallets:
        pallet_type = str(pallet.get("pallet_type") or "UNKNOWN")
        sales_order_no = str(pallet.get("sales_order_no") or "UNKNOWN_ORDER")
        key = f"{pallet_type}__{sales_order_no}"
        counters[key] = counters.get(key, 0) + 1
        pallet["pallet_id"] = f"{pallet_type}-{sales_order_no}-{counters[key]}"


def _build_merged_report(
    pallets: List[Dict],
    initial_report: Dict,
    incremental_report: Dict,
    repack_box_count: int,
    new_box_count: int,
    total_runtime: float,
    rescue_stats: Dict = None,
) -> Dict:
    summary = _summarize_pallets(pallets)
    return {
        "packing_plan_id": None,
        "mode": "incremental",
        "total_runtime_seconds": round(total_runtime, 2),
        "incremental": {
            "initial_pallets": len(initial_report.get("pallets", [])),
            "initial_success_pallets_kept": sum(
                1
                for pallet in initial_report.get("pallets", [])
                if pallet.get("mpm_status") == "SUCCESS"
            ),
            "initial_repack_box_count": repack_box_count,
            "new_box_count": new_box_count,
            "incremental_pallets": len(incremental_report.get("pallets", [])),
            **(rescue_stats or {}),
        },
        "summary": {"overall": summary, "by_pallet_type": {}},
        "pallets": pallets,
    }


def _summarize_pallets(pallets: List[Dict]) -> Dict:
    failed = [p for p in pallets if p.get("mpm_status") == "FAILED"]
    gaps = [max(0.0, float(p.get("mpm_gap") or 0.0)) for p in failed]
    return {
        "total_pallets": len(pallets),
        "success_pallets": sum(1 for p in pallets if p.get("mpm_status") == "SUCCESS"),
        "failed_pallets": len(failed),
        "unknown_pallets": sum(1 for p in pallets if p.get("mpm_status") == "UNKNOWN"),
        "avg_mpm_gap": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        "max_mpm_gap": max(gaps) if gaps else 0.0,
    }


def _empty_report() -> Dict:
    return {
        "packing_plan_id": None,
        "total_runtime_seconds": 0.0,
        "summary": {"overall": _summarize_pallets([]), "by_pallet_type": {}},
        "pallets": [],
    }
