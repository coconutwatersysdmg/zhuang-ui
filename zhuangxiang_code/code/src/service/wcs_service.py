"""WCS 接口装箱常驻服务（HTTP 服务壳）。

两条独立流水线（互不等待）：

1. 拉取器：每 download_interval 秒 POST 接口 1；
   原始响应 → ``input/raw/``；
   过滤 MH423C 后 → ``input/pending/``（未加入计算）。
2. 装箱器：监听 ``input/pending/``。仅当该目录出现新文件时开算——
   合并当前全部 pending + 上一轮 FAILED 结转，写入 ``input/packing_inputs/``，
   用过的 pending 移到 ``input/consumed/``；算完若 pending 无新变化则暂停，
   避免仅用不达标结转箱反复空转。

可选：装箱结果推送接口 2（由 ``_PUSH_PLAN_TO_WCS`` 控制）。
"""

from __future__ import annotations

import json
import threading
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
# 装箱器空闲时轮询间隔（秒）：无 pending 且无结转时短暂等待。
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
    # TODO(拉取间隔): 接口1轮询间隔（秒）。这里是代码兜底默认值；
    # 正式以 packing_config.yaml → data_source.download_interval 为准。
    # 仅由拉取线程使用；装箱线程不算完不睡这个间隔。
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


def _load_json(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter_mh423c(stock_entries: List[Dict]) -> Tuple[List[Dict], int, List[str]]:
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


def _product_code_key(value) -> Optional[str]:
    """规范化 product_code；空值返回 None（无法按码去重，原样保留）。"""
    if value is None or value == "":
        return None
    return str(value).strip()


def _dedupe_by_product_code(
    items: List[Dict],
    *,
    keep_last: bool = True,
) -> Tuple[List[Dict], int]:
    """按 product_code 去重；无 product_code 的条目全部保留。

    Args:
        items: 库存品类或已展开的箱子列表。
        keep_last: True=同码保留后出现的（多份 pending 时偏新）；
            False=保留先出现的（合并后 api 优先于结转）。

    Returns:
        (deduped_list, dropped_duplicate_count)
    """
    if keep_last:
        by_code: Dict[str, Dict] = {}
        no_code: List[Dict] = []
        for item in items:
            key = _product_code_key(item.get("product_code"))
            if key is None:
                no_code.append(item)
            else:
                by_code[key] = item
        deduped = no_code + list(by_code.values())
        dropped = len(items) - len(deduped)
        return deduped, max(0, dropped)

    seen: set = set()
    deduped = []
    dropped = 0
    for item in items:
        key = _product_code_key(item.get("product_code"))
        if key is None:
            deduped.append(item)
            continue
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, dropped


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
    """WCS 接口模式常驻服务：拉取线程 + 装箱线程独立运行。"""

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
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        # 拉取写入 pending 后唤醒装箱线程（监听 pending 目录变化）
        self._pending_wake = threading.Event()
        self._ensure_dirs()
        self._reload_reference_data()

    # ------------------------------------------------------------------ dirs
    @property
    def pending_dir(self) -> Path:
        """未加入计算的库存 JSON。"""
        return self._ds.input_dir / "pending"

    @property
    def consumed_dir(self) -> Path:
        """已加入计算的库存 JSON。"""
        return self._ds.input_dir / "consumed"

    @property
    def raw_dir(self) -> Path:
        """接口 1 原始响应备份。"""
        return self._ds.input_dir / "raw"

    @property
    def packing_inputs_dir(self) -> Path:
        return self._ds.input_dir / "packing_inputs"

    @property
    def bad_dir(self) -> Path:
        return self._ds.input_dir / "bad"

    def _ensure_dirs(self) -> None:
        for d in (
            self._ds.input_dir,
            self.pending_dir,
            self.consumed_dir,
            self.raw_dir,
            self.packing_inputs_dir,
            self.bad_dir,
            self._ds.output_dir,
        ):
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
    def fetch_once(self) -> Optional[Path]:
        """拉一次接口 1，过滤后写入 pending（未加入计算）。返回 pending 路径。"""
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

        pending_body = {
            "timestamp": ts,
            "compute_status": "pending",  # 未加入计算
            "code": stock_body.get("code", 0),
            "msg": stock_body.get("msg", "ok"),
            "data": kept,
            "filter": {
                "kept_case_type": _SUPPORTED_CASE_TYPE,
                "raw_count": len(stock_entries),
                "kept_count": len(kept),
                "dropped_count": dropped,
                "dropped_types": dropped_types,
            },
        }
        pending_path = self.pending_dir / f"{ts}.json"
        with self._pending_lock:
            _save_json(pending_path, pending_body)
        print(f"[WCS-拉] 未加入计算 → {pending_path}")
        self._pending_wake.set()  # 通知装箱线程：pending 有新数据
        return pending_path

    def _list_pending_files(self) -> List[Path]:
        with self._pending_lock:
            files = sorted(self.pending_dir.glob("*.json"))
        return files

    def _pending_snapshot(self) -> Tuple[Tuple[str, int], ...]:
        """pending 目录快照 (文件名, mtime_ns)，用于判断是否有新接口数据。"""
        with self._pending_lock:
            out = []
            for p in sorted(self.pending_dir.glob("*.json")):
                try:
                    out.append((p.name, p.stat().st_mtime_ns))
                except OSError:
                    out.append((p.name, 0))
            return tuple(out)

    def _mark_pending_consumed(self, paths: List[Path], pack_ts: str) -> None:
        """把本轮用过的 pending 标成已加入计算，并移到 consumed/。"""
        with self._pending_lock:
            for src in paths:
                if not src.exists():
                    continue
                try:
                    body = _load_json(src)
                except (OSError, json.JSONDecodeError):
                    body = {}
                body["compute_status"] = "consumed"  # 已加入计算
                body["consumed_at"] = pack_ts
                dst = self.consumed_dir / src.name
                if dst.exists():
                    dst = self.consumed_dir / f"{src.stem}_{pack_ts}{src.suffix}"
                _save_json(dst, body)
                try:
                    src.unlink()
                except OSError:
                    pass
                print(f"[WCS-装] 已加入计算 → {dst}")

    # ------------------------------------------------------------------ pack
    def pack_once(self) -> bool:
        """消化当前全部 pending + 结转箱，装箱一轮。

        仅当 ``pending/`` 有文件时才开算（必须有新的接口数据）；
        不允许仅用结转不达标箱单独反复计算。

        Returns:
            True=本轮执行了装箱流程；False=pending 为空，应暂停等待目录变化。
        """
        pending_files = self._list_pending_files()
        carry_in = list(self._carry_boxes)
        # 关键：没有新接口数据就不算，避免不达标箱空转重算
        if not pending_files:
            return False

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        print(f"\n{'=' * 60}")
        print(
            f"[WCS-装] {ts}：pending={len(pending_files)} 份，"
            f"结转={len(carry_in)} 箱"
        )

        stock_entries: List[Dict] = []
        source_names: List[str] = []
        for path in pending_files:
            try:
                body = _load_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[WCS-装] 读取 pending 失败，跳过 {path.name}：{exc}")
                continue
            entries = body.get("data") or []
            stock_entries.extend(entries)
            source_names.append(path.name)

        # 已确定本轮要吃掉的 pending：先标 consumed，避免与拉取线程竞态重复消费
        if pending_files:
            self._mark_pending_consumed(pending_files, ts)

        # 多份 pending 合并后按 product_code 去重（同码保留较新的一份）
        raw_entry_count = len(stock_entries)
        stock_entries, stock_dup_dropped = _dedupe_by_product_code(
            stock_entries, keep_last=True
        )
        if stock_dup_dropped:
            print(
                f"[WCS-装] pending 按 product_code 去重：{raw_entry_count} → "
                f"{len(stock_entries)}（去掉重复 {stock_dup_dropped}）"
            )

        pallet_dims = default_pallet_dims_map(self._config_path)
        api_boxes = stock_to_boxes(stock_entries, self._bms_map, pallet_dims)
        boxes, carry_for_input = _merge_api_and_carry_boxes(api_boxes, carry_in, ts)
        # pending 与结转之间再去重：同码优先保留 pending（先出现），丢掉结转重复
        before_box_count = len(boxes)
        boxes, box_dup_dropped = _dedupe_by_product_code(boxes, keep_last=False)
        if box_dup_dropped:
            print(
                f"[WCS-装] 与结转按 product_code 去重：{before_box_count} → "
                f"{len(boxes)}（去掉重复 {box_dup_dropped}）"
            )
        print(
            f"[WCS-装] 本轮输入：pending 展开 {len(api_boxes)} 箱 + "
            f"结转 {len(carry_for_input)} 箱 → 去重后合计 {len(boxes)} 箱"
            f"（来源文件：{source_names or '无'}）"
        )

        packing_input_path = self.packing_inputs_dir / f"{ts}.json"
        _save_json(
            packing_input_path,
            {
                "timestamp": ts,
                "pending_files": source_names,
                "from_api_count": len(api_boxes),
                "from_carry_count": len(carry_for_input),
                "stock_dup_dropped": stock_dup_dropped,
                "box_dup_dropped": box_dup_dropped,
                "merged_count": len(boxes),
                "from_api": api_boxes,
                "from_carry": carry_for_input,
                "boxes": boxes,
            },
        )
        print(f"[WCS-装] 算法输入已保存：{packing_input_path}")

        if not boxes:
            print("[WCS-装] 合并后无箱，跳过装箱。")
            return True

        try:
            workflow = self._make_workflow()
            report = workflow.run_with_boxes(boxes)
        except Exception as exc:
            print(f"[WCS-装] 装箱异常：{exc}；本轮输入退回结转池，避免丢箱。")
            self._carry_boxes = boxes
            bad_path = self.bad_dir / f"pack_{ts}.json"
            _save_json(
                bad_path,
                {"timestamp": ts, "error": str(exc), "pending_files": source_names},
            )
            return True

        if report is None:
            print("[WCS-装] 装箱失败（无有效报告）；本轮输入退回结转池，避免丢箱。")
            self._carry_boxes = boxes
            return True

        self._carry_boxes = _extract_repack_boxes(report)
        failed_pallets = sum(
            1
            for p in (report.get("pallets") or [])
            if p.get("mpm_status") == "FAILED"
        )
        print(
            f"[WCS-装] 不达标盘 {failed_pallets} 个，"
            f"结转箱 {len(self._carry_boxes)} 个；"
            f"若 pending 无新文件将暂停，等待下一次接口数据。"
        )

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
        """拉取线程：立即首拉，之后每 download_interval 秒一次。"""
        while not self._stop.is_set():
            try:
                self.fetch_once()
            except Exception as exc:
                print(f"[WCS-拉] 本轮拉取异常：{exc}")
            # TODO(拉取间隔): 仅拉取线程按秒等待；与装箱无关
            if self._stop.wait(self._ds.download_interval):
                break

    def _pack_loop(self) -> None:
        """装箱线程：监听 pending/；有新接口数据才算，算完无变化则暂停。"""
        idle_announced = False
        while not self._stop.is_set():
            snap = self._pending_snapshot()
            if snap:
                idle_announced = False
                try:
                    self._reload_reference_data()
                    self.pack_once()
                except Exception as exc:
                    print(f"[WCS-装] 循环异常：{exc}")
                # 若装箱期间又写入了新 pending，下一圈立刻继续；否则进入等待
                continue

            # pending 无文件：停止计算（结转箱待命，不单独重算）
            if not idle_announced:
                print(
                    f"[WCS-装] 监听 {self.pending_dir}：暂无新接口数据，"
                    f"暂停计算（结转待命 {len(self._carry_boxes)} 箱）。"
                )
                idle_announced = True
            self._pending_wake.clear()
            # 被拉取线程 set，或超时后再看一眼目录（防止漏信号）
            self._pending_wake.wait(timeout=_PACK_IDLE_POLL_SEC)

    def run_loop(self) -> None:
        """启动拉取 + 装箱双线程，直到 Ctrl+C / stop。"""
        print("=" * 60)
        print("WCS 接口装箱服务（双流水线）")
        print(f"  接口地址：{self._ds.api_base_url}")
        print(f"  拉取间隔：{self._ds.download_interval} 秒（仅拉取线程）")
        print(f"  接口原始：{self.raw_dir}")
        print(f"  未加入计算（监听）：{self.pending_dir}")
        print(f"  已加入计算：{self.consumed_dir}")
        print(f"  算法输入：{self.packing_inputs_dir}")
        print(f"  输出目录：{self._ds.output_dir}")
        print(f"  BMS 参考：{self._ds.bms_reference_file}")
        print("  装箱：pending 有新数据才算；无变化则暂停（结转不单独空转）")
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
            self._pending_wake.set()
            fetch_thread.join(timeout=5)
            pack_thread.join(timeout=5)
            print("[WCS] 服务已结束。")

    # 兼容旧入口：单次串行（调试用）
    def run_once(self) -> bool:
        """调试：拉一次 + 装一次（串行）。正式常驻请用 run_loop。"""
        try:
            self.fetch_once()
        except Exception as exc:
            print(f"[WCS] run_once 拉取失败：{exc}")
            return False
        return self.pack_once()
