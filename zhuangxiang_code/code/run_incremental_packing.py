"""Run independent incremental packing tests on staged-order Excel files."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from run_packing import build_workflow
from src.incremental import load_incremental_excel, run_incremental_packing
from src.main.report_persister import NullReportPersister


DEFAULT_FILES = [
    "data/selected_6000_boxes_9_1_concentrated_3orders.xlsx",
    "data/selected_7000_boxes_9_1_concentrated_3orders.xlsx",
    "data/selected_8000_boxes_9_1_concentrated_3orders.xlsx",
    "data/selected_9000_boxes_9_1_concentrated_3orders.xlsx",
    "data/selected_10000_boxes_9_1_concentrated_3orders.xlsx",
]


def _workflow_factory():
    workflow = build_workflow()
    workflow._report_persister = NullReportPersister()
    return workflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", default=DEFAULT_FILES)
    parser.add_argument("--output-dir", default="output/incremental")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    initial_report_cache = {}
    initial_cache_dir = output_dir / "cache"
    initial_cache_dir.mkdir(parents=True, exist_ok=True)
    for file_name in args.files:
        path = Path(file_name)
        print("=" * 80)
        print(f"Incremental packing test: {path}")
        batch = load_incremental_excel(path)
        stem = path.stem
        report_path = output_dir / f"{stem}_incremental_report.json"
        if report_path.exists():
            print(f"Reusing existing incremental report: {report_path}")
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            row = _summary_row(path, batch, report)
            rows.append(row)
            continue

        cache_key = _boxes_signature(batch.initial_boxes)
        initial_report = initial_report_cache.get(cache_key)
        if initial_report is None:
            cache_path = initial_cache_dir / f"initial_{_signature_digest(cache_key)}.json"
            if cache_path.exists():
                print(f"Loading cached initial order packing plan: {cache_path}")
                with open(cache_path, "r", encoding="utf-8") as f:
                    initial_report = json.load(f)
            else:
                print("Calculating initial order packing plan.")
                workflow = _workflow_factory()
                initial_report = workflow.run_with_boxes(batch.initial_boxes)
                if initial_report is None:
                    raise RuntimeError(f"Initial packing returned no report: {path}")
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(initial_report, f, indent=2, ensure_ascii=False)
            initial_report_cache[cache_key] = initial_report
        else:
            print("Reusing cached initial order packing plan.")

        result = run_incremental_packing(
            batch.initial_boxes,
            batch.new_boxes,
            _workflow_factory,
            initial_report=initial_report,
        )
        report = result.report
        row = _summary_row(
            path,
            batch,
            report,
            runtime_seconds=result.total_runtime_seconds,
        )
        rows.append(row)
        print(
            "Done: "
            f"initial_boxes={row['initial_boxes']}, "
            f"new_boxes={row['new_boxes']}, "
            f"initial_repack_boxes={row['initial_repack_boxes']}, "
            f"failed_recovered={row['initial_failed_recovered_pallets']}"
            f"/{row['initial_failed_pallets']}, "
            f"pallets={row['total_pallets']}, "
            f"success={row['success_pallets']}, "
            f"failed={row['failed_pallets']}, "
            f"runtime={row['runtime_seconds']}s"
        )

        with open(
            report_path,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    df = pd.DataFrame(rows)
    summary_path = output_dir / "incremental_test_summary.xlsx"
    df.to_excel(summary_path, index=False)
    csv_path = output_dir / "incremental_test_summary.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print("=" * 80)
    print(f"Summary Excel: {summary_path}")
    print(f"Summary CSV: {csv_path}")
    return 0


def _boxes_signature(boxes):
    return tuple(sorted(str(box.get("id")) for box in boxes))


def _signature_digest(signature):
    data = "\n".join(signature).encode("utf-8")
    return hashlib.sha1(data).hexdigest()[:16]


def _summary_row(path, batch, report, runtime_seconds=None):
    overall = report["summary"]["overall"]
    incremental = report.get("incremental", {})
    return {
        "file": str(path),
        "initial_boxes": len(batch.initial_boxes),
        "new_boxes": len(batch.new_boxes),
        "initial_repack_boxes": incremental.get("initial_repack_box_count", ""),
        "initial_failed_pallets": incremental.get("initial_failed_pallet_count", ""),
        "initial_failed_recovered_pallets": incremental.get(
            "initial_failed_recovered_pallets", ""
        ),
        "initial_failed_boxes_in_success": incremental.get(
            "initial_failed_boxes_in_success", ""
        ),
        "total_pallets": overall["total_pallets"],
        "success_pallets": overall["success_pallets"],
        "failed_pallets": overall["failed_pallets"],
        "avg_mpm_gap": overall["avg_mpm_gap"],
        "max_mpm_gap": overall["max_mpm_gap"],
        "runtime_seconds": (
            runtime_seconds
            if runtime_seconds is not None
            else report.get("total_runtime_seconds", "")
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
