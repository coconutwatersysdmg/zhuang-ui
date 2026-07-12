"""WCS 接口装箱常驻服务（HTTP 服务壳）。

流程（每轮）：
  1. POST 接口 1 reqstockinfo → 原始 JSON 落盘 input/
  2. wcs_adapter.stock_to_boxes → run_with_boxes 装箱
  3. 完整报告 + 接口 2 case 数组落盘 output/
  4. POST 接口 2 sendpalletplanresult
  5. 打印 [UI-RESULT] 供可视化 UI 自动刷新

进程常驻，按 download_interval 秒循环，直到 Ctrl+C 或 UI 停止。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
from src.main.report_persister import NullReportPersister

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CODE_ROOT = Path(__file__).resolve().parents[2]

# TODO 输入输出地址
WCS_STOCK_PATH = "/adaptor/api/wcs/reqstockinfo"
WCS_PLAN_PATH = "/adaptor/api/wcs/sendpalletplanresult"

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
        """执行一轮：拉库存 → 装箱 → 本地落盘 → 推接口 2。返回是否成功完成装箱推送。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_dir = self._ds.input_dir
        processed_dir = input_dir / "processed"
        bad_dir = input_dir / "bad"
        for d in (input_dir, processed_dir, bad_dir, self._ds.output_dir):
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
            pallet_dims = default_pallet_dims_map(self._config_path)
            boxes = stock_to_boxes(stock_entries, self._bms_map, pallet_dims)
            print(f"[WCS] 展开为 {len(boxes)} 个箱子。")

            if not boxes:
                print("[WCS] 库存为空，跳过装箱与接口 2 推送。")
                shutil.move(str(stock_path), str(processed_dir / stock_path.name))
                return True

            workflow = self._make_workflow()
            report = workflow.run_with_boxes(boxes)
            if report is None:
                print("[WCS] 装箱失败（无有效报告）。")
                shutil.move(str(stock_path), str(bad_dir / stock_path.name))
                return False

            report_path = self._ds.output_dir / f"packing_plan_{ts}.json"
            _save_json(report_path, report)
            print(f"[WCS] 装箱报告已保存：{report_path}")

            plan = report_to_plan_result(report)
            plan_path = self._ds.output_dir / f"wcs_plan_{ts}.json"
            _save_json(plan_path, plan.cases)
            print(f"[WCS] 接口 2 发送体已保存：{plan_path}（{len(plan.cases)} 个 case）")

            push_body = push_plan_result(self._ds.api_base_url, plan.cases)
            print(
                f"[WCS] 接口 2 推送成功：code={push_body.get('code')}, "
                f"msg={push_body.get('msg')}"
            )

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
