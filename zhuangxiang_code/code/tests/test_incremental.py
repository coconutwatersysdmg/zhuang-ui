"""Tests for the independent incremental packing adapter."""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.incremental import run_incremental_packing


def _box(box_id, mpm=10):
    return {
        "id": box_id,
        "type": "T",
        "length": 100,
        "width": 100,
        "height": 100,
        "weight": 1,
        "min_pack_multiple": mpm,
        "pallet_type": "A",
        "sales_order_no": "O1",
        "pallet_dims": {"length": 1000, "width": 1000, "height": 1000},
    }


def _placed(box):
    placed = dict(box)
    placed["position"] = {"x": 0, "y": 0, "z": 0}
    placed["volume"] = placed["length"] * placed["width"] * placed["height"]
    return placed


class StubWorkflow:
    def __init__(self):
        self.calls = []

    def run_with_boxes(self, boxes):
        ids = [box["id"] for box in boxes]
        self.calls.append(ids)
        if len(self.calls) == 1:
            return {
                "total_runtime_seconds": 1.0,
                "summary": {"overall": {"success_pallets": 1, "failed_pallets": 1}},
                "pallets": [
                    {
                        "pallet_id": "A-O1-1",
                        "pallet_type": "A",
                        "sales_order_no": "O1",
                        "mpm_total": 20,
                        "mpm_target": 20,
                        "mpm_gap": 0,
                        "mpm_status": "SUCCESS",
                        "stability_checks": {"status": "SUCCESS"},
                        "packed_items": [_placed(boxes[0])],
                    },
                    {
                        "pallet_id": "A-O1-2",
                        "pallet_type": "A",
                        "sales_order_no": "O1",
                        "mpm_total": 10,
                        "mpm_target": 20,
                        "mpm_gap": 10,
                        "mpm_status": "FAILED",
                        "stability_checks": {"status": "SUCCESS"},
                        "packed_items": [_placed(boxes[1])],
                    },
                ],
            }
        return {
            "total_runtime_seconds": 2.0,
            "summary": {"overall": {"success_pallets": 1, "failed_pallets": 1}},
            "pallets": [
                {
                    "pallet_id": "A-O1-1",
                    "pallet_type": "A",
                    "sales_order_no": "O1",
                    "mpm_total": 20,
                    "mpm_target": 20,
                    "mpm_gap": 0,
                    "mpm_status": "SUCCESS",
                    "stability_checks": {"status": "SUCCESS"},
                    "packed_items": [_placed(boxes[0]), _placed(boxes[1])],
                }
            ],
        }


def test_incremental_reuses_failed_pallet_boxes_with_new_boxes():
    workflow = StubWorkflow()
    initial = [_box("old-success", 20), _box("old-failed", 10)]
    additions = [_box("new-1", 10)]

    result = run_incremental_packing(
        initial,
        additions,
        lambda: workflow,
    )

    assert workflow.calls == [
        ["old-success", "old-failed"],
        ["old-failed", "new-1"],
    ]
    assert result.initial_repack_box_count == 1
    assert result.new_box_count == 1
    assert result.report["summary"]["overall"]["total_pallets"] == 2
    output_ids = {
        item["id"]
        for pallet in result.report["pallets"]
        for item in pallet["packed_items"]
    }
    assert output_ids == {"old-success", "old-failed", "new-1"}

    # 首跑未达标托盘救回统计：1 个未达标托盘，其箱子 old-failed 在二次
    # 装箱中落到了达标托盘上 → 救回 1 托盘 / 1 箱。
    inc = result.report["incremental"]
    assert inc["initial_failed_pallet_count"] == 1
    assert inc["initial_failed_recovered_pallets"] == 1
    assert inc["initial_failed_boxes_in_success"] == 1


class StubWorkflowOldBoxNotRescued:
    """二次装箱中旧箱仍落在未达标托盘上：救回数必须为 0。"""

    def __init__(self):
        self.calls = []

    def run_with_boxes(self, boxes):
        ids = [box["id"] for box in boxes]
        self.calls.append(ids)
        if len(self.calls) == 1:
            return {
                "total_runtime_seconds": 1.0,
                "summary": {"overall": {"success_pallets": 1, "failed_pallets": 1}},
                "pallets": [
                    {
                        "pallet_id": "A-O1-1",
                        "pallet_type": "A",
                        "sales_order_no": "O1",
                        "mpm_total": 20,
                        "mpm_target": 20,
                        "mpm_gap": 0,
                        "mpm_status": "SUCCESS",
                        "stability_checks": {"status": "SUCCESS"},
                        "packed_items": [_placed(boxes[0])],
                    },
                    {
                        "pallet_id": "A-O1-2",
                        "pallet_type": "A",
                        "sales_order_no": "O1",
                        "mpm_total": 10,
                        "mpm_target": 20,
                        "mpm_gap": 10,
                        "mpm_status": "FAILED",
                        "stability_checks": {"status": "SUCCESS"},
                        "packed_items": [_placed(boxes[1])],
                    },
                ],
            }
        new_box = next(box for box in boxes if box["id"] == "new-1")
        old_box = next(box for box in boxes if box["id"] == "old-failed")
        return {
            "total_runtime_seconds": 2.0,
            "summary": {"overall": {"success_pallets": 1, "failed_pallets": 1}},
            "pallets": [
                {
                    "pallet_id": "A-O1-1",
                    "pallet_type": "A",
                    "sales_order_no": "O1",
                    "mpm_total": 20,
                    "mpm_target": 20,
                    "mpm_gap": 0,
                    "mpm_status": "SUCCESS",
                    "stability_checks": {"status": "SUCCESS"},
                    "packed_items": [_placed(new_box)],
                },
                {
                    "pallet_id": "A-O1-2",
                    "pallet_type": "A",
                    "sales_order_no": "O1",
                    "mpm_total": 10,
                    "mpm_target": 20,
                    "mpm_gap": 10,
                    "mpm_status": "FAILED",
                    "stability_checks": {"status": "SUCCESS"},
                    "packed_items": [_placed(old_box)],
                },
            ],
        }


def test_recovered_stat_zero_when_old_boxes_stay_failed():
    workflow = StubWorkflowOldBoxNotRescued()
    initial = [_box("old-success", 20), _box("old-failed", 10)]
    additions = [_box("new-1", 20)]

    result = run_incremental_packing(
        initial,
        additions,
        lambda: workflow,
    )

    inc = result.report["incremental"]
    assert inc["initial_failed_pallet_count"] == 1
    # 达标托盘只含新增箱，不含首跑未达标箱 → 救回 0 托盘 / 0 箱。
    assert inc["initial_failed_recovered_pallets"] == 0
    assert inc["initial_failed_boxes_in_success"] == 0


if __name__ == "__main__":
    test_incremental_reuses_failed_pallet_boxes_with_new_boxes()
    test_recovered_stat_zero_when_old_boxes_stay_failed()
    print("[PASS] incremental tests")
