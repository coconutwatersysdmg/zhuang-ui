"""WCS 接口装箱常驻服务（HTTP 服务壳）。

流程（每轮）：
  1. POST 接口 1 reqstockinfo → 原始 JSON 落盘 input/
  2. 过滤 MH423C → stock_to_boxes，并与上一轮不达标结转箱合并
  3. 本轮算法输入落盘 input/packing_inputs/
  4. run_with_boxes 装箱；FAILED 盘箱子结转至下一轮
  5. 完整报告 + 接口 2 case 数组落盘 output/
  6. （可选）POST 接口 2 sendpalletplanresult
  7. 打印 [UI-RESULT] 供可视化 UI 自动刷新

进程常驻，按 download_interval 秒循环，直到 Ctrl+C 或 UI 停止。
"""

from __future__ import annotations

import json
import shutil
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

from src.adapter import (
    default_pallet_dims_map,
    load_bms_map,
    report_to_plan_result,
    stock_to_boxes,
)
from src.adapter.wcs_adapter import build_stock_request
from src.config import DATA_DIR, OUTPUT_DIR, ConfigLoader
from src.incremental.service import _extract_repack_boxes
from src.main.report_persister import NullReportPersister

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CODE_ROOT = Path(__file__).resolve().parents[2]

# 接口模式下仅处理该托盘型号；其余 case_type 在装箱前剔除。
_SUPPORTED_CASE_TYPE = "MH423C"
# False=只本地装箱/落盘，不向接口 2 推送结果（调试用，恢复推送改 True）。
_PUSH_PLAN_TO_WCS = False

# TODO 输入输出地址
# 接口1
WCS_STOCK_PATH = "/api/wcs/reqstockinfo"
# 接口2
WCS_PLAN_PATH = "ssss/api/wcs/sendpalletplanresult"

_DEFAULT_DATA_SOURCE = {
    "mode": "api",
    # TODO(接口地址): 兜底的Mock地址
    "api_base_url": "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
    "download_interval": 200,
    "input_dir": "input",
    "bms_reference_file": "668箱子数据集.xlsx",
}


@dataclass(frozen=True)
class DataSourceConfig:
    mode: str
    api_base_url: str
    download_interval: int
    input_dir: Path
    bms_reference_file: Path
    output_dir: Path


def load_data_source_config(config_path: Optional[Path] = None) -> DataSourceConfig:
    """从 yaml 的 data_source 段读取接口模式配置。"""
    merged = dict(_DEFAULT_DATA_SOURCE)
    if config_path and Path(config_path).exists():
        try:
            raw = (ConfigLoader(Path(config_path)).config_data or {}).get("data_source") or {}
            merged.update({k: v for k, v in raw.items() if v is not None})
        except (OSError, ValueError, KeyError):
            pass
    rel_input = str(merged.get("input_dir", "input") or "input")
    bms_rel = str(
        merged.get("bms_reference_file", _DEFAULT_DATA_SOURCE["bms_reference_file"])
        or _DEFAULT_DATA_SOURCE["bms_reference_file"]
    )
    return DataSourceConfig(
        mode=str(merged.get("mode", "api")).strip().lower(),
        api_base_url=str(merged.get("api_base_url") or _DEFAULT_DATA_SOURCE["api_base_url"]),
        download_interval=max(1, int(merged.get("download_interval", 200) or 200)),
        input_dir=(_CODE_ROOT / rel_input).resolve(),
        bms_reference_file=(DATA_DIR / bms_rel).resolve(),
        output_dir=OUTPUT_DIR.resolve(),
    )


def fetch_stock_response(base_url: str, timeout: int = 30) -> Dict:
    """POST 接口 1，返回完整响应体 {code, msg, data}。"""
    url = f"{base_url.rstrip('/')}{WCS_STOCK_PATH}"
    resp = requests.post(
        url,
        json=build_stock_request(),
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"接口 1 返回错误: code={body.get('code')}, msg={body.get('msg')}"
        )
    return body


def push_plan_result(base_url: str, cases: List[Dict], timeout: int = 60) -> Dict:
    """POST 接口 2，发送 case 数组，返回 WCS 响应体。"""
    url = f"{base_url.rstrip('/')}{WCS_PLAN_PATH}"
    resp = requests.post(url, json=cases, timeout=timeout, verify=False)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"接口 2 返回错误: code={body.get('code')}, msg={body.get('msg')}"
        )
    return body


def _save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _merge_api_and_carry_boxes(
    api_boxes: List[Dict],
    carry_boxes: List[Dict],
    round_tag: str,
) -> Tuple[List[Dict], List[Dict]]:
    """合并本轮接口箱与上一轮结转箱；冲突时改写结转箱 id。

    Returns:
        (merged_boxes, carry_boxes_with_final_ids)
    """
    merged: List[Dict] = [deepcopy(b) for b in api_boxes]
    used_ids = {str(b.get("id")) for b in merged if b.get("id") is not None}
    carry_out: List[Dict] = []
    for i, src in enumerate(carry_boxes):
        box = deepcopy(src)
        bid = str(box.get("id") or f"CARRY-{i}")
        if bid in used_ids:
            bid = f"CARRY-{round_tag}-{i}-{bid}"
            box["id"] = bid
        used_ids.add(bid)
        box.setdefault("id", bid)
        carry_out.append(box)
        merged.append(box)
    return merged, carry_out


class WcsPackingService:
    """WCS 接口模式常驻装箱服务。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        safe_compare: bool = False,
    ):
        from run_packing import build_workflow, load_constraint_config

        self._config_path = Path(config_path) if config_path else None
        self._ds = load_data_source_config(self._config_path)
        self._safe_compare = safe_compare
        self._constraint_config = load_constraint_config(self._config_path)
        self._build_workflow = build_workflow
        self._bms_map: Dict[str, float] = {}
        # 上一轮 mpm_status=FAILED 托盘上的箱子，并入下一轮算法输入。
        self._carry_boxes: List[Dict] = []
        self._reload_reference_data()

    def _reload_reference_data(self) -> None:
        bms_path = self._ds.bms_reference_file
        if bms_path.exists():
            self._bms_map = load_bms_map(bms_path)
            print(f"[WCS] 已加载 BMS 参考表：{bms_path}")
        else:
            self._bms_map = {}
            print(f"[WCS] 警告：BMS 参考文件不存在：{bms_path}，指数将按 0 处理。")

    def _make_workflow(self):
        wf = self._build_workflow(
            safe_compare=self._safe_compare,
            constraint_config=self._constraint_config,
        )
        wf._report_persister = NullReportPersister()
        return wf

    def run_once(self) -> bool:
        """执行一轮：拉库存 → 合并结转 → 装箱 → 本地落盘 →（可选）推接口 2。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_dir = self._ds.input_dir
        processed_dir = input_dir / "processed"
        bad_dir = input_dir / "bad"
        # 剔除非 MH423C 后的库存，便于对照原始 JSON
        filtered_dir = input_dir / "filtered_mh423c"
        # 每轮真正喂给算法的输入（接口新箱 + 结转不达标箱）
        packing_inputs_dir = input_dir / "packing_inputs"
        for d in (
            input_dir,
            processed_dir,
            bad_dir,
            filtered_dir,
            packing_inputs_dir,
            self._ds.output_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        stock_path = input_dir / f"{ts}.json"
        try:
            print(f"\n{'=' * 60}")
            print(f"[WCS] 第 {ts} 轮：拉取接口 1 …")
            stock_body = fetch_stock_response(self._ds.api_base_url)
            _save_json(stock_path, stock_body)
            print(f"[WCS] 接口 1 原始响应已保存：{stock_path}")

            stock_entries = stock_body.get("data") or []
            print(f"[WCS] 库存品类数：{len(stock_entries)}")

            kept = [
                e for e in stock_entries
                if str(e.get("case_type") or "").strip() == _SUPPORTED_CASE_TYPE
            ]
            dropped = len(stock_entries) - len(kept)
            if dropped:
                dropped_types = sorted({
                    str(e.get("case_type"))
                    for e in stock_entries
                    if str(e.get("case_type") or "").strip() != _SUPPORTED_CASE_TYPE
                })
                print(
                    f"[WCS] 已剔除 case_type≠{_SUPPORTED_CASE_TYPE} 的品类 "
                    f"{dropped} 条（类型：{dropped_types}），"
                    f"保留 {len(kept)} 条参与装箱。"
                )
            stock_entries = kept

            filtered_body = {
                "code": stock_body.get("code", 0),
                "msg": stock_body.get("msg", "ok"),
                "data": stock_entries,
                "filter": {
                    "kept_case_type": _SUPPORTED_CASE_TYPE,
                    "raw_count": len(stock_body.get("data") or []),
                    "kept_count": len(stock_entries),
                    "dropped_count": dropped,
                },
            }
            filtered_path = filtered_dir / f"{ts}.json"
            _save_json(filtered_path, filtered_body)
            print(f"[WCS] 剔除后库存已保存：{filtered_path}")

            pallet_dims = default_pallet_dims_map(self._config_path)
            api_boxes = stock_to_boxes(stock_entries, self._bms_map, pallet_dims)
            carry_in = list(self._carry_boxes)
            boxes, carry_for_input = _merge_api_and_carry_boxes(
                api_boxes, carry_in, ts
            )
            print(
                f"[WCS] 本轮输入：接口展开 {len(api_boxes)} 箱 + "
                f"结转不达标 {len(carry_for_input)} 箱 → 合计 {len(boxes)} 箱。"
            )

            packing_input_path = packing_inputs_dir / f"{ts}.json"
            _save_json(
                packing_input_path,
                {
                    "timestamp": ts,
                    "from_api_count": len(api_boxes),
                    "from_carry_count": len(carry_for_input),
                    "merged_count": len(boxes),
                    "from_api": api_boxes,
                    "from_carry": carry_for_input,
                    "boxes": boxes,
                },
            )
            print(f"[WCS] 本轮算法输入已保存：{packing_input_path}")

            if not boxes:
                print("[WCS] 库存与结转均为空，跳过装箱。")
                shutil.move(str(stock_path), str(processed_dir / stock_path.name))
                return True

            workflow = self._make_workflow()
            report = workflow.run_with_boxes(boxes)
            if report is None:
                print("[WCS] 装箱失败（无有效报告）；结转池保持不变。")
                shutil.move(str(stock_path), str(bad_dir / stock_path.name))
                return False

            # 本轮 FAILED 盘箱子 → 下一轮结转（SUCCESS 视为已消化）
            self._carry_boxes = _extract_repack_boxes(report)
            failed_pallets = sum(
                1
                for p in (report.get("pallets") or [])
                if p.get("mpm_status") == "FAILED"
            )
            print(
                f"[WCS] 本轮不达标盘 {failed_pallets} 个，"
                f"结转箱 {len(self._carry_boxes)} 个供下一轮。"
            )

            report_path = self._ds.output_dir / f"packing_plan_{ts}.json"
            _save_json(report_path, report)
            print(f"[WCS] 装箱报告已保存：{report_path}")

            plan = report_to_plan_result(report)
            plan_path = self._ds.output_dir / f"wcs_plan_{ts}.json"
            _save_json(plan_path, plan.cases)
            print(f"[WCS] 接口 2 发送体已保存：{plan_path}（{len(plan.cases)} 个 case）")

            if _PUSH_PLAN_TO_WCS:
                push_body = push_plan_result(self._ds.api_base_url, plan.cases)
                print(
                    f"[WCS] 接口 2 推送成功：code={push_body.get('code')}, "
                    f"msg={push_body.get('msg')}"
                )
            else:
                print("[WCS] 已跳过接口 2 推送（仅本地保存）。")

            map_path = self._ds.output_dir / f"wcs_plan_map_{ts}.json"
            _save_json(
                map_path,
                {uid: pallet for uid, pallet in plan.plan_by_unique_id.items()},
            )
            print(f"[WCS] box_unique_id 映射已保存：{map_path}")

            print(f"[UI-RESULT] {report_path.resolve()}")
            shutil.move(str(stock_path), str(processed_dir / stock_path.name))
            return True

        except Exception as exc:
            print(f"[WCS] 本轮异常：{exc}")
            if stock_path.exists():
                try:
                    shutil.move(str(stock_path), str(bad_dir / stock_path.name))
                except Exception:
                    pass
            return False

    def run_loop(self) -> None:
        """常驻循环：立即执行首轮，之后每 download_interval 秒一轮。"""
        print("=" * 60)
        print("WCS 接口装箱服务（常驻模式）")
        print(f"  接口地址：{self._ds.api_base_url}")
        print(f"  拉取间隔：{self._ds.download_interval} 秒")
        print(f"  库存目录：{self._ds.input_dir}")
        print(f"  输出目录：{self._ds.output_dir}")
        print(f"  BMS 参考：{self._ds.bms_reference_file}")
        print("  结转：上一轮 FAILED 盘箱子并入下一轮输入")
        if self._config_path:
            print(f"  约束配置：{self._config_path}")
        print("  按 Ctrl+C 或由 UI 停止按钮结束进程")
        print("=" * 60)

        try:
            while True:
                self._reload_reference_data()
                self.run_once()
                print(f"[WCS] 等待 {self._ds.download_interval} 秒后下一轮 …")
                for _ in range(self._ds.download_interval):
                    time.sleep(1)
        except KeyboardInterrupt:
            print("[WCS] 收到停止信号，服务已结束。")
