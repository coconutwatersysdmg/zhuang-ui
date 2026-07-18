"""Pure helpers for selecting the order used by 3D playback."""

from __future__ import annotations

from typing import Dict, List


ROBOT_MODE_LABEL = "机器人执行顺序"
ORIGINAL_MODE_LABEL = "原算法顺序"


def sequence_mode_key(value: object) -> str:
    text = str(value or "").strip()
    if text in {"robot", ROBOT_MODE_LABEL}:
        return "robot"
    return "original"


def _positive_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def ordered_packed_items(pallet: Dict, mode: object = "robot") -> List[Dict]:
    """Return a new list ordered for playback without mutating the report."""
    items = list((pallet or {}).get("packed_items", []) or [])
    indexed = list(enumerate(items, start=1))
    mode_key = sequence_mode_key(mode)

    if mode_key == "robot" and str((pallet or {}).get("sequence_status")) == "GEOMETRICALLY_FEASIBLE":
        robot_values = [_positive_int(item.get("robot_packing_sequence")) for _, item in indexed]
        if all(value is not None for value in robot_values) and len(set(robot_values)) == len(robot_values):
            return [
                item
                for _, item in sorted(
                    indexed,
                    key=lambda pair: (
                        _positive_int(pair[1].get("robot_packing_sequence")) or 10**9,
                        pair[0],
                    ),
                )
            ]

    return [
        item
        for _, item in sorted(
            indexed,
            key=lambda pair: (
                _positive_int(pair[1].get("original_packing_sequence")) or pair[0],
                pair[0],
            ),
        )
    ]
