from copy import deepcopy

import pytest

from src.postprocess.robot_sequence import (
    apply_robot_sequences,
    plan_pallet_robot_sequence,
    topological_sequence,
)


def box(box_id, x, y, z, lx=300, ly=300, lz=100):
    return {
        "id": box_id,
        "length": float(lx),
        "width": float(ly),
        "height": float(lz),
        "position": {"x": float(x), "y": float(y), "z": float(z)},
        "pallet_dims": {"length": 1440, "width": 2240, "height": 720},
    }


def success_pallet(items):
    return {
        "pallet_id": "P-1",
        "mpm_status": "SUCCESS",
        "packed_items": deepcopy(items),
    }


def test_fixed_corner_and_far_to_near_order_from_robot_origin():
    pallet = success_pallet([
        box("far", 900, 1200, 0),
        box("near", 0, 0, 0),
        box("middle", 400, 300, 0),
    ])

    result = plan_pallet_robot_sequence(pallet)

    assert result["sequence_status"] == "GEOMETRICALLY_FEASIBLE"
    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["far"]["robot_packing_sequence"] == 1
    assert by_id["middle"]["robot_packing_sequence"] == 2
    assert by_id["near"]["robot_packing_sequence"] == 3
    for item in pallet["packed_items"]:
        assert item["suction_box_corner"] == "x_min_y_min"
        assert item["suction_cup_corner"] == "x_min_y_min"
        assert item["suction_cup_x_size"] == 600.0
        assert item["suction_cup_y_size"] == 800.0
        assert item["suction_rect_x_min"] == item["position"]["x"]
        assert item["suction_rect_y_min"] == item["position"]["y"]


def test_supporters_are_always_before_supported_box():
    lower = box("lower", 0, 0, 0, lx=500, ly=500, lz=100)
    upper = box("upper", 50, 50, 100, lx=300, ly=300, lz=100)
    pallet = success_pallet([upper, lower])

    plan_pallet_robot_sequence(pallet)

    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["lower"]["robot_packing_sequence"] == 1
    assert by_id["upper"]["robot_packing_sequence"] == 2
    assert by_id["upper"]["support_predecessors"] == ["lower"]
    assert "lower" in by_id["upper"]["all_predecessors"]


def test_tall_suction_blocker_is_scheduled_after_low_target():
    low_target = box("low", 0, 0, 0, lx=250, ly=250, lz=100)
    tall_blocker = box("tall", 500, 0, 0, lx=250, ly=250, lz=300)
    pallet = success_pallet([tall_blocker, low_target])

    plan_pallet_robot_sequence(pallet)

    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["low"]["robot_packing_sequence"] < by_id["tall"]["robot_packing_sequence"]
    assert "low" in by_id["tall"]["clearance_predecessors"] or "tall" in by_id["low"]["clearance_successors"]


def test_cycle_is_reported_and_no_robot_sequence_is_emitted():
    order, remaining = topological_sequence(
        node_ids=["A", "B"],
        predecessors={"A": {"B"}, "B": {"A"}},
        score_key=lambda node_id, placed: (node_id,),
    )

    assert order == []
    assert remaining == ["A", "B"]


def test_failed_pallet_is_skipped_without_changing_geometry():
    pallet = {
        "pallet_id": "P-F",
        "mpm_status": "FAILED",
        "packed_items": [box("A", 10, 20, 0)],
    }
    original = deepcopy(pallet["packed_items"])

    result = plan_pallet_robot_sequence(pallet)

    assert result["sequence_status"] == "SKIPPED_FAILED_PALLET"
    assert pallet["packed_items"] == original


def test_report_summary_counts_successful_and_infeasible_pallets():
    report = {
        "pallets": [
            success_pallet([box("A", 0, 0, 0)]),
            {"pallet_id": "P-F", "mpm_status": "FAILED", "packed_items": [box("B", 0, 0, 0)]},
        ]
    }

    summary = apply_robot_sequences(report["pallets"])

    assert summary["successful_pallets_processed"] == 1
    assert summary["geometrically_feasible_pallets"] == 1
    assert summary["skipped_failed_pallets"] == 1
    assert summary["suction_cup_size_mm"] == [600.0, 800.0]


def test_result_formatter_attaches_robot_sequence_summary():
    from src.main.result_formatter import ResultFormatter

    raw = [box("A", 0, 0, 0)]
    plan = success_pallet(raw)
    report = ResultFormatter.build_full_report(
        [plan],
        {"overall": {}, "by_pallet_type": {}},
        1.25,
        raw,
        lambda final_plan, raw_boxes: deepcopy(final_plan),
    )

    assert report["robot_sequence_summary"]["successful_pallets_processed"] == 1
    assert report["pallets"][0]["sequence_status"] == "GEOMETRICALLY_FEASIBLE"
    assert report["pallets"][0]["packed_items"][0]["robot_packing_sequence"] == 1


def test_successful_postprocess_preserves_final_geometry_and_list_order():
    pallet = success_pallet([
        box("A", 10, 20, 0, lx=330, ly=440, lz=120),
        box("B", 500, 700, 0, lx=250, ly=350, lz=200),
    ])
    before = [
        (
            item["id"],
            deepcopy(item["position"]),
            item["length"],
            item["width"],
            item["height"],
        )
        for item in pallet["packed_items"]
    ]

    plan_pallet_robot_sequence(pallet)

    after = [
        (
            item["id"],
            deepcopy(item["position"]),
            item["length"],
            item["width"],
            item["height"],
        )
        for item in pallet["packed_items"]
    ]
    assert after == before


def test_far_unfinished_region_is_completed_before_independent_near_boxes():
    pallet = success_pallet([
        box("near", 0, 0, 0, lx=500, ly=500, lz=250),
        box("far_upper", 940, 1640, 100, lx=260, ly=260, lz=100),
        box("middle", 500, 850, 0, lx=300, ly=300, lz=100),
        box("far_lower", 900, 1600, 0, lx=400, ly=400, lz=100),
    ])

    plan_pallet_robot_sequence(pallet)

    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["far_lower"]["robot_packing_sequence"] == 1
    # The middle box is a mandatory suction-clearance predecessor of far_upper.
    assert by_id["middle"]["robot_packing_sequence"] == 2
    assert by_id["far_upper"]["robot_packing_sequence"] == 3
    assert by_id["near"]["robot_packing_sequence"] == 4
    assert pallet["sequence_strategy"] == "robot_relative_far_to_near_retreat"
    assert pallet["robot_reference"] == "x_min_y_min"
    assert pallet["depth_policy"] == "farthest_unfinished_band_locked"


def test_far_target_dependency_chain_beats_unrelated_near_candidate():
    support = box("wide_support", 380, 980, 0, lx=900, ly=1050, lz=100)
    far_target = box("far_target", 980, 1740, 100, lx=200, ly=200, lz=100)
    unrelated_near = box("unrelated_near", 0, 0, 0, lx=500, ly=500, lz=300)
    pallet = success_pallet([unrelated_near, far_target, support])

    plan_pallet_robot_sequence(pallet)

    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["wide_support"]["robot_packing_sequence"] == 1
    assert by_id["far_target"]["robot_packing_sequence"] == 2
    assert by_id["unrelated_near"]["robot_packing_sequence"] == 3
    assert "wide_support" in by_id["far_target"]["support_predecessors"]


def test_robot_reference_can_be_changed_to_y_max_side():
    pallet = success_pallet([
        box("near_y_max", 300, 1900, 0),
        box("far_y_min", 300, 100, 0),
    ])

    plan_pallet_robot_sequence(pallet, robot_reference="y_max")

    by_id = {item["id"]: item for item in pallet["packed_items"]}
    assert by_id["far_y_min"]["robot_packing_sequence"] == 1
    assert by_id["near_y_max"]["robot_packing_sequence"] == 2
    assert by_id["far_y_min"]["robot_depth"] > by_id["near_y_max"]["robot_depth"]
    assert pallet["robot_reference"] == "y_max"
