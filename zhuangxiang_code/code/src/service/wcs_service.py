"""WCS 接口装箱常驻服务（HTTP 服务壳）。

两条独立流水线（互不等待）：

1. 拉取器：每 download_interval 秒 POST 接口 1；
   原始 JSON → ``input/raw/``（本地仅保留此目录）；
   过滤 MH423C 后按 ``product_code`` 插入 ``zhuangdb.wcs_stock_box``
   （已存在则跳过；新行 ``up_to_standard=0``）。仅当有新插入时唤醒装箱。

2. 装箱器：被新插入唤醒后，读取库中全部未达标行作为算法输入；
   算完把 SUCCESS 盘箱子的 ``up_to_standard`` 更新为 1；未达标保持 0。
   无新插入则暂停，避免未达标箱反复空转。

可选：装箱结果推送接口 2（由 ``_PUSH_PLAN_TO_WCS`` 控制）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

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
from src.service.stock_db import (
    WcsStockRepository,
    load_database_config,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CODE_ROOT = Path(__file__).resolve().parents[2]

# 接口模式下仅处理该托盘型号；其余 case_type 在落库前剔除。
_SUPPORTED_CASE_TYPE = "MH423C"
# False=只本地装箱/落盘，不向接口 2 推送结果（调试用，恢复推送改 True）。
_PUSH_PLAN_TO_WCS = False
# 装箱器空闲等待超时（秒）：防止漏掉 wake 信号。
_PACK_IDLE_POLL_SEC = 2.0

# TODO 输入输出地址
# 接口1
WCS_STOCK_PATH = "/api/wcs/reqstockinfo"
# 接口2
WCS_PLAN_PATH = "ssss/api/wcs/sendpalletplanresult"

_DEFAULT_DATA_SOURCE = {
    "mode": "api",
    # TODO(接口地址): 兜底的Mock地址
    "api_base_url": "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
    # TODO(拉取间隔): 接口1轮询间隔（秒）。仅拉取线程使用。
    "download_interval": 200,
    "input_dir": "input",
    "bms_reference_file": "668箱子数据集.xlsx",
}

_DEFAULT_DATABASE = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    # TODO(数据库密码): 与 packing_config.yaml → database.password 保持一致；现场改正式密码
    "password": "123456",
    "database": "zhuangdb",
    "charset": "utf8mb4",
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


def _load_db_config_from_yaml(config_path: Optional[Path] = None):
    merged = dict(_DEFAULT_DATABASE)
    if config_path and Path(config_path).exists():
        try:
            raw = (ConfigLoader(Path(config_path)).config_data or {}).get("database") or {}
            merged.update({k: v for k, v in raw.items() if v is not None})
        except (OSError, ValueError, KeyError):
            pass
    return load_database_config(merged)


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


def _filter_mh423c(stock_entries: List[Dict]):
    """保留 case_type=MH423C；返回 (kept, dropped_count, dropped_types)。"""
    kept = [
        e for e in stock_entries
        if str(e.get("case_type") or "").strip() == _SUPPORTED_CASE_TYPE
    ]
    dropped = len(stock_entries) - len(kept)
    dropped_types = sorted({
        str(e.get("case_type"))
        for e in stock_entries
        if str(e.get("case_type") or "").strip() != _SUPPORTED_CASE_TYPE
    })
    return kept, dropped, dropped_types


def _success_product_codes(report: Optional[Dict]) -> Set[int]:
    """从装箱报告中收集 SUCCESS 托盘上的 product_code。"""
    codes: Set[int] = set()
    for pallet in (report or {}).get("pallets") or []:
        if pallet.get("mpm_status") != "SUCCESS":
            continue
        for item in pallet.get("packed_items") or []:
            pc = item.get("product_code")
            if pc is None or pc == "":
                continue
            try:
                codes.add(int(pc))
            except (TypeError, ValueError):
                continue
    return codes


class WcsPackingService:
    """WCS 接口模式常驻服务：拉取落库 + 读库装箱。"""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        safe_compare: bool = False,
    ):
        from run_packing import build_workflow, load_constraint_config

        self._config_path = Path(config_path) if config_path else None
        self._ds = load_data_source_config(self._config_path)
        self._db_cfg = _load_db_config_from_yaml(self._config_path)
        self._repo = WcsStockRepository(self._db_cfg)
        self._safe_compare = safe_compare
        self._constraint_config = load_constraint_config(self._config_path)
        self._build_workflow = build_workflow
        self._bms_map: Dict[str, float] = {}
        self._stop = threading.Event()
        # 仅有新插入时置位，装箱线程据此开算
        self._db_insert_wake = threading.Event()
        self._ensure_dirs()
        self._reload_reference_data()

    @property
    def raw_dir(self) -> Path:
        """接口 1 原始响应（input 下唯一保留的数据目录）。"""
        return self._ds.input_dir / "raw"

    @property
    def bad_dir(self) -> Path:
        return self._ds.input_dir / "bad"

    def _ensure_dirs(self) -> None:
        for d in (self._ds.input_dir, self.raw_dir, self.bad_dir, self._ds.output_dir):
            d.mkdir(parents=True, exist_ok=True)

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

    # ------------------------------------------------------------------ fetch
    def fetch_once(self) -> int:
        """拉一次接口 1：原始 JSON 落 raw/，新箱子插入 DB。返回新插入行数。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        print(f"\n{'=' * 60}")
        print(f"[WCS-拉] {ts}：拉取接口 1 …")
        stock_body = fetch_stock_response(self._ds.api_base_url)

        raw_path = self.raw_dir / f"{ts}.json"
        _save_json(raw_path, stock_body)
        print(f"[WCS-拉] 原始响应已保存：{raw_path}")

        stock_entries = stock_body.get("data") or []
        kept, dropped, dropped_types = _filter_mh423c(stock_entries)
        if dropped:
            print(
                f"[WCS-拉] 已剔除 case_type≠{_SUPPORTED_CASE_TYPE} 的品类 "
                f"{dropped} 条（类型：{dropped_types}），保留 {len(kept)} 条。"
            )
        else:
            print(f"[WCS-拉] 库存品类数：{len(kept)}（均为 {_SUPPORTED_CASE_TYPE}）")

        inserted = self._repo.insert_new_stock_entries(kept)
        print(
            f"[WCS-拉] 落库完成：候选 {len(kept)} 条，新插入 {inserted} 条"
            f"（已存在 product_code 已跳过，up_to_standard=0）。"
        )
        if inserted > 0:
            self._db_insert_wake.set()
            print("[WCS-拉] 有新插入 → 唤醒装箱线程。")
        else:
            print("[WCS-拉] 无新插入 → 不触发装箱。")
        return inserted

    # ------------------------------------------------------------------ pack
    def pack_once(self) -> bool:
        """从 DB 读全部未达标箱子装箱；达标行回写 up_to_standard=1。

        Returns:
            True=执行了装箱（或明确跳过空输入）；False=无未达标数据。
        """
        rows = self._repo.fetch_unmet_rows()
        if not rows:
            print("[WCS-装] 库中无未达标箱子，跳过。")
            return False

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        print(f"\n{'=' * 60}")
        print(f"[WCS-装] {ts}：未达标行 {len(rows)} 条，开始装箱 …")

        stock_entries = self._repo.rows_to_stock_entries(rows)
        pallet_dims = default_pallet_dims_map(self._config_path)
        boxes = stock_to_boxes(stock_entries, self._bms_map, pallet_dims)
        print(f"[WCS-装] 展开为 {len(boxes)} 个箱子。")

        if not boxes:
            print("[WCS-装] 展开后无箱，跳过。")
            return True

        try:
            workflow = self._make_workflow()
            report = workflow.run_with_boxes(boxes)
        except Exception as exc:
            print(f"[WCS-装] 装箱异常：{exc}")
            bad_path = self.bad_dir / f"pack_{ts}.json"
            _save_json(bad_path, {"timestamp": ts, "error": str(exc)})
            return True

        if report is None:
            print("[WCS-装] 装箱失败（无有效报告）。")
            return True

        success_codes = _success_product_codes(report)
        updated = self._repo.mark_standard_by_product_codes(success_codes)
        failed_pallets = sum(
            1
            for p in (report.get("pallets") or [])
            if p.get("mpm_status") == "FAILED"
        )
        print(
            f"[WCS-装] SUCCESS 产品码 {len(success_codes)} 个，"
            f"DB 更新达标 {updated} 行；FAILED 盘 {failed_pallets} 个保持未达标。"
        )
        print("[WCS-装] 本轮结束；等待下一次「新插入」再开算。")

        report_path = self._ds.output_dir / f"packing_plan_{ts}.json"
        _save_json(report_path, report)
        print(f"[WCS-装] 装箱报告已保存：{report_path}")

        plan = report_to_plan_result(report)
        plan_path = self._ds.output_dir / f"wcs_plan_{ts}.json"
        _save_json(plan_path, plan.cases)
        print(f"[WCS-装] 接口 2 发送体已保存：{plan_path}（{len(plan.cases)} 个 case）")

        if _PUSH_PLAN_TO_WCS:
            try:
                push_body = push_plan_result(self._ds.api_base_url, plan.cases)
                print(
                    f"[WCS-装] 接口 2 推送成功：code={push_body.get('code')}, "
                    f"msg={push_body.get('msg')}"
                )
            except Exception as exc:
                print(f"[WCS-装] 接口 2 推送失败：{exc}")
        else:
            print("[WCS-装] 已跳过接口 2 推送（仅本地保存）。")

        map_path = self._ds.output_dir / f"wcs_plan_map_{ts}.json"
        _save_json(
            map_path,
            {uid: pallet for uid, pallet in plan.plan_by_unique_id.items()},
        )
        print(f"[UI-RESULT] {report_path.resolve()}")
        return True

    # ------------------------------------------------------------------ loops
    def _fetch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.fetch_once()
            except Exception as exc:
                print(f"[WCS-拉] 本轮拉取异常：{exc}")
            # TODO(拉取间隔): 仅拉取线程按秒等待
            if self._stop.wait(self._ds.download_interval):
                break

    def _pack_loop(self) -> None:
        """仅在有新插入时装箱；算完后暂停直到下一次插入。"""
        idle_announced = False
        while not self._stop.is_set():
            # 等待「有新插入」信号
            if not self._db_insert_wake.is_set():
                if not idle_announced:
                    print(
                        "[WCS-装] 等待数据库新插入（有新 product_code 才开算）…"
                    )
                    idle_announced = True
                self._db_insert_wake.wait(timeout=_PACK_IDLE_POLL_SEC)
                if self._stop.is_set():
                    break
                if not self._db_insert_wake.is_set():
                    continue

            idle_announced = False
            self._db_insert_wake.clear()
            try:
                self._reload_reference_data()
                self.pack_once()
            except Exception as exc:
                print(f"[WCS-装] 循环异常：{exc}")
            # 算完不自动连算；若装箱期间又有新插入，wake 会被再次 set，下一圈继续

    def run_loop(self) -> None:
        print("=" * 60)
        print("WCS 接口装箱服务（DB 模式）")
        print(f"  接口地址：{self._ds.api_base_url}")
        print(f"  拉取间隔：{self._ds.download_interval} 秒（仅拉取）")
        print(f"  原始 JSON：{self.raw_dir}")
        print(
            f"  数据库：{self._db_cfg.host}:{self._db_cfg.port}/"
            f"{self._db_cfg.database} 表 wcs_stock_box"
        )
        print(f"  BMS 参考：{self._ds.bms_reference_file}")
        print(f"  输出目录：{self._ds.output_dir}")
        print("  触发：仅「新插入」开算；达标回写 up_to_standard=1")
        if self._config_path:
            print(f"  约束配置：{self._config_path}")
        print("  按 Ctrl+C 或由 UI 停止按钮结束进程")
        print("=" * 60)

        fetch_thread = threading.Thread(
            target=self._fetch_loop, name="wcs-fetch", daemon=True
        )
        pack_thread = threading.Thread(
            target=self._pack_loop, name="wcs-pack", daemon=True
        )
        fetch_thread.start()
        pack_thread.start()
        try:
            while fetch_thread.is_alive() or pack_thread.is_alive():
                fetch_thread.join(timeout=0.5)
                pack_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            print("[WCS] 收到停止信号，正在结束 …")
            self._stop.set()
            self._db_insert_wake.set()
            fetch_thread.join(timeout=5)
            pack_thread.join(timeout=5)
            print("[WCS] 服务已结束。")

    def run_once(self) -> bool:
        """调试：拉一次 + 若有新插入则装一次。"""
        try:
            inserted = self.fetch_once()
        except Exception as exc:
            print(f"[WCS] run_once 拉取失败：{exc}")
            return False
        if inserted <= 0:
            print("[WCS] run_once：无新插入，不装箱。")
            return True
        return self.pack_once()
