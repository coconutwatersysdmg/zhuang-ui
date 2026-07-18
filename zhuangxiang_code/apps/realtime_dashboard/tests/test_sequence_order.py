from sequence_order import ordered_packed_items, sequence_mode_key


def test_robot_mode_sorts_by_robot_sequence_without_mutating_source():
    items = [
        {"id": "A", "robot_packing_sequence": 2, "original_packing_sequence": 1},
        {"id": "B", "robot_packing_sequence": 1, "original_packing_sequence": 2},
    ]
    pallet = {
        "sequence_status": "GEOMETRICALLY_FEASIBLE",
        "packed_items": items,
    }

    ordered = ordered_packed_items(pallet, "robot")

    assert [item["id"] for item in ordered] == ["B", "A"]
    assert [item["id"] for item in items] == ["A", "B"]


def test_robot_mode_falls_back_to_original_when_sequence_is_unavailable():
    pallet = {
        "sequence_status": "INFEASIBLE_FIXED_CORNER",
        "packed_items": [
            {"id": "A", "original_packing_sequence": 2},
            {"id": "B", "original_packing_sequence": 1},
        ],
    }

    ordered = ordered_packed_items(pallet, "robot")

    assert [item["id"] for item in ordered] == ["B", "A"]


def test_chinese_combo_labels_map_to_internal_modes():
    assert sequence_mode_key("机器人执行顺序") == "robot"
    assert sequence_mode_key("原算法顺序") == "original"
