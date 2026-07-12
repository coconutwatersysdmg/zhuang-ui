# -*- coding: utf-8 -*-
r"""
Industrial friendly realtime packing dashboard v2.

Place this file in:
    zhuangxiang_code/apps/realtime_dashboard/realtime_dashboard_v2.py

Run:
    python apps/realtime_dashboard/realtime_dashboard_v2.py
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import math
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# -----------------------------------------------------------------------------
# Qt runtime fix: set plugin path before importing PyQt5.
# -----------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_THIS_DIR = _THIS_FILE.parent
_PROJECT_DIR_DEFAULT = _THIS_DIR.parents[1]          # .../zhuangxiang_code
_WORKSPACE_DIR_DEFAULT = _PROJECT_DIR_DEFAULT.parent # .../zhuang-ui
_VENV_DIR_DEFAULT = _WORKSPACE_DIR_DEFAULT / ".venvs" / "packing-realtime"
_QT_PLUGIN_DIR = _VENV_DIR_DEFAULT / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins"
_QT_PLATFORM_DIR = _QT_PLUGIN_DIR / "platforms"

if _QT_PLATFORM_DIR.exists():
    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(_QT_PLATFORM_DIR))
if _QT_PLUGIN_DIR.exists():
    os.environ.setdefault("QT_PLUGIN_PATH", str(_QT_PLUGIN_DIR))

from PyQt5 import QtCore, QtGui, QtWidgets

if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:
    from stability_business_dashboard_json import (
        MainWindow as BaseDashboard,
        MetricCard,
        ColorBarWidget,
        safe_float,
        safe_str,
        score_level,
        blue_red_rgba,
        categorical_rgba,
    )
except Exception as exc:
    raise RuntimeError(
        "Cannot import stability_business_dashboard_json.py. "
        "Please keep it in apps/realtime_dashboard and make sure the syntax patch has been applied."
    ) from exc


DEFAULT_CONFIG_REL = Path(r"code\config\packing_config.yaml")
DEFAULT_RUN_SCRIPT_REL = Path(r"code\run_packing.py")
RUNTIME_NAME = "packing-realtime"


# 左侧“装箱参数方案”只影响前端稳定性复核，不改后端装箱算法。
# 选择方案后会自动写入下面 4 个评价参数，并刷新当前托盘分析结果。
PARAMETER_SCHEMES = {
    "标准方案": {"z_tol": 5.0, "ax": 0.40, "ay": 0.30, "mu": 0.45, "desc": "普通仓储/展示场景"},
    "保守方案": {"z_tol": 3.0, "ax": 0.60, "ay": 0.50, "mu": 0.30, "desc": "评价更严格，适合先排风险"},
    "运输抗震": {"z_tol": 4.0, "ax": 0.80, "ay": 0.60, "mu": 0.35, "desc": "考虑运输振动和急停"},
    "高效率": {"z_tol": 8.0, "ax": 0.30, "ay": 0.25, "mu": 0.55, "desc": "偏展示空间利用，评价较宽松"},
}


IGNORED_RISK_PHRASES = {"吸盘矩形越界"}


def normalize_risk_text(text: object) -> str:
    """Remove prompts that should not be treated as real装箱风险."""
    raw = safe_str(text, "正常")
    if raw in {"", "--", "正常"}:
        return "正常"
    parts = []
    for part in raw.replace(";", "；").split("；"):
        part = part.strip()
        if not part or part in IGNORED_RISK_PHRASES:
            continue
        parts.append(part)
    return "；".join(parts) if parts else "正常"


def workspace_dir_from_project(project_dir: Path) -> Path:
    return Path(project_dir).resolve().parent


def runtime_dir_from_project(project_dir: Path) -> Path:
    return workspace_dir_from_project(project_dir) / "runtime" / RUNTIME_NAME


def log_dir_from_project(project_dir: Path) -> Path:
    return runtime_dir_from_project(project_dir) / "logs"


def ensure_runtime_dirs(project_dir: Path) -> None:
    runtime_dir = runtime_dir_from_project(project_dir)
    for p in [runtime_dir, runtime_dir / "logs", runtime_dir / "exports", runtime_dir / "temp"]:
        p.mkdir(parents=True, exist_ok=True)


def find_latest_json(project_dir: Path) -> Optional[Path]:
    project_dir = Path(project_dir).resolve()
    workspace_dir = workspace_dir_from_project(project_dir)
    candidates: List[Path] = []
    search_roots = [
        project_dir / "output",
        project_dir / "outputs",
        project_dir / "code" / "output",
        project_dir / "code" / "outputs",
        project_dir,
        workspace_dir / "runtime" / RUNTIME_NAME / "exports",
    ]
    patterns = ["packing_plan_*.json", "*packing*.json", "*.json"]
    for root in search_roots:
        if not root.exists():
            continue
        for pattern in patterns:
            candidates.extend([p for p in root.rglob(pattern) if p.is_file()])

    filtered: List[Path] = []
    for p in candidates:
        low = str(p).lower()
        if "config" in low or "__pycache__" in low or "site-packages" in low:
            continue
        filtered.append(p)
    return max(filtered, key=lambda p: p.stat().st_mtime) if filtered else None


class PackingWorker(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    started_cmd = QtCore.pyqtSignal(str)
    finished_json = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, project_dir: Path, config_path: Path, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir).resolve()
        self.config_path = Path(config_path).resolve()
        self.process: Optional[subprocess.Popen] = None
        self._stop_requested = False
        ensure_runtime_dirs(self.project_dir)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = log_dir_from_project(self.project_dir) / f"backend_{stamp}.log"

    def stop(self) -> None:
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def _write_backend_log(self, text: str) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def run(self) -> None:
        try:
            run_script = self.project_dir / DEFAULT_RUN_SCRIPT_REL
            if not run_script.exists():
                self.failed.emit(f"找不到装箱算法入口：{run_script}")
                return
            if not self.config_path.exists():
                self.failed.emit(f"找不到配置文件：{self.config_path}")
                return

            before_latest = find_latest_json(self.project_dir)
            before_mtime = before_latest.stat().st_mtime if before_latest else 0.0

            cmd = [sys.executable, str(run_script), "--config", str(self.config_path)]
            cmd_text = " ".join(f'"{x}"' if " " in x else x for x in cmd)
            self.started_cmd.emit(cmd_text)
            self.log.emit(f"[LOG] 后端日志文件：{self.log_file}")
            self._write_backend_log(f"[CMD] {cmd_text}")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            creationflags = 0
            preexec_fn = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                preexec_fn = os.setsid

            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if self._stop_requested:
                    self.log.emit("[UI] 已请求停止后端装箱。")
                    self._write_backend_log("[UI] 已请求停止后端装箱。")
                    return
                msg = line.rstrip()
                if msg:
                    self.log.emit(msg)
                    self._write_backend_log(msg)

            code = self.process.wait()
            if self._stop_requested:
                self.log.emit("[UI] 后端装箱已停止。")
                self._write_backend_log("[UI] 后端装箱已停止。")
                return
            if code != 0:
                self.failed.emit(f"装箱算法运行失败，退出码：{code}")
                return

            time.sleep(0.4)
            latest = find_latest_json(self.project_dir)
            if latest is None:
                self.failed.emit("算法运行完成，但没有找到输出 JSON。请检查 output / outputs 目录。")
                return
            if latest.stat().st_mtime < before_mtime:
                self.log.emit(f"[提醒] 找到的 JSON 可能不是本次新生成文件：{latest}")
            self.finished_json.emit(str(latest))
        except Exception as exc:
            self.failed.emit(str(exc))


class StatusPill(QtWidgets.QLabel):
    def __init__(self, text: str = "空闲", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumWidth(92)
        self.set_state("idle")

    def set_state(self, state: str, text: Optional[str] = None) -> None:
        if text is not None:
            self.setText(text)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class StepCard(QtWidgets.QFrame):
    def __init__(self, number: str, title: str, desc: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("StepCard")
        self.setProperty("state", "normal")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self.badge = QtWidgets.QLabel(number)
        self.badge.setObjectName("StepBadge")
        self.badge.setFixedSize(26, 26)
        self.badge.setAlignment(QtCore.Qt.AlignCenter)
        txt = QtWidgets.QVBoxLayout()
        txt.setSpacing(2)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("StepTitle")
        self.desc = QtWidgets.QLabel(desc)
        self.desc.setObjectName("StepDesc")
        self.desc.setWordWrap(True)
        txt.addWidget(self.title)
        txt.addWidget(self.desc)
        layout.addWidget(self.badge)
        layout.addLayout(txt, 1)

    def set_state(self, state: str, desc: Optional[str] = None) -> None:
        self.setProperty("state", state)
        if desc is not None:
            self.desc.setText(desc)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class SummaryKpi(QtWidgets.QFrame):
    def __init__(self, title: str, value: str = "--", unit: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("SummaryKpi")
        self.setProperty("state", "normal")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("KpiTitle")
        self.value = QtWidgets.QLabel(value)
        self.value.setObjectName("KpiValue")
        self.unit = QtWidgets.QLabel(unit)
        self.unit.setObjectName("KpiUnit")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.unit)

    def set_data(self, value: str, unit: str = "", state: str = "normal") -> None:
        self.value.setText(value)
        self.unit.setText(unit)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class PalletPreviewCanvas(QtWidgets.QWidget):
    """独立 3D 托盘视图。

    每个托盘卡片内部都有自己的 GLViewWidget，可以拖动、缩放、旋转。
    """

    clicked = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(210)
        self.pallet = None
        self.scene_items = []
        self.has_gl = False
        self.gl = None
        self.np = None
        self.show_suction = True
        self.only_risk = False
        self.color_mode = "按支撑风险着色"
        self.visible_count: Optional[int] = None
        self.selected_box_key = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        try:
            import numpy as _np
            import pyqtgraph.opengl as _gl

            self.np = _np
            self.gl = _gl
            self.has_gl = True
            self.view = _gl.GLViewWidget()
            self.view.setBackgroundColor("#F8FAFC")
            self.view.opts["distance"] = 2500
            self.view.opts["elevation"] = 28
            self.view.opts["azimuth"] = -55
            self._last_pallet_key = None
            self._press_pos = None
            self._dragging_view = False

            # 区分“点击选择托盘”和“拖动旋转/平移视角”。
            # 之前在 mousePress 时就触发 clicked，导致用户拖动视角也会重新加载托盘，
            # 进一步触发 3D 视图刷新和默认相机恢复。现在改为：只有左键按下并且
            # 松开时移动距离很小，才认为是“点击选择”。中键/拖动不触发选择。
            old_press = self.view.mousePressEvent
            old_move = self.view.mouseMoveEvent
            old_release = self.view.mouseReleaseEvent

            def _press(ev):
                self._press_pos = ev.pos()
                self._dragging_view = False
                old_press(ev)

            def _move(ev):
                if self._press_pos is not None:
                    try:
                        if (ev.pos() - self._press_pos).manhattanLength() > 6:
                            self._dragging_view = True
                    except Exception:
                        self._dragging_view = True
                old_move(ev)

            def _release(ev):
                try:
                    is_left = ev.button() == QtCore.Qt.LeftButton
                    moved = False
                    if self._press_pos is not None:
                        moved = (ev.pos() - self._press_pos).manhattanLength() > 6
                    if is_left and (not self._dragging_view) and (not moved):
                        self.clicked.emit()
                finally:
                    self._press_pos = None
                    self._dragging_view = False
                    old_release(ev)

            self.view.mousePressEvent = _press
            self.view.mouseMoveEvent = _move
            self.view.mouseReleaseEvent = _release
            layout.addWidget(self.view)
        except Exception:
            self.view = QtWidgets.QLabel("缺少 3D 依赖")
            self.view.setAlignment(QtCore.Qt.AlignCenter)
            self.view.setObjectName("PreviewSub")
            layout.addWidget(self.view)

    def set_pallet(self, pallet):
        new_key = safe_str((pallet or {}).get("pallet_id"), "") if pallet else ""
        same_pallet = bool(new_key and new_key == getattr(self, "_last_pallet_key", None))
        self.pallet = pallet
        self._last_pallet_key = new_key
        self.visible_count = None
        # 同一个托盘因刷新状态/右侧数据/重新着色而重绘时，保留用户相机角度；
        # 切换到新的托盘时，使用默认角度，避免沿用上一个托盘的异常视角。
        self.render(preserve_camera=same_pallet)

    def set_options(self, show_suction: Optional[bool] = None, only_risk: Optional[bool] = None,
                    color_mode: Optional[str] = None, visible_count: Optional[int] = None,
                    reset_camera: bool = False):
        if show_suction is not None:
            self.show_suction = bool(show_suction)
        if only_risk is not None:
            self.only_risk = bool(only_risk)
        if color_mode is not None:
            self.color_mode = str(color_mode)
        self.visible_count = visible_count
        self.render(preserve_camera=not reset_camera)

    def _clear(self):
        if not self.has_gl:
            return
        for item in self.scene_items:
            try:
                self.view.removeItem(item)
            except Exception:
                pass
        self.scene_items = []

    def _camera_state(self):
        if not self.has_gl:
            return None
        state = {}
        for key in ["distance", "elevation", "azimuth", "center", "fov"]:
            if key in self.view.opts:
                state[key] = self.view.opts.get(key)
        return state

    def _restore_camera_state(self, state) -> None:
        if not self.has_gl or not state:
            return
        try:
            self.view.opts.update(state)
            self.view.update()
        except Exception:
            pass

    def _set_default_camera(self, L: float, W: float, H: float) -> None:
        if not self.has_gl:
            return
        try:
            self.view.setCameraPosition(distance=max(L, W, H) * 1.55, elevation=28, azimuth=-55)
        except Exception:
            self.view.opts["distance"] = max(L, W, H) * 1.55
            self.view.opts["elevation"] = 28
            self.view.opts["azimuth"] = -55
            self.view.update()

    def reset_camera(self) -> None:
        if not self.has_gl:
            return
        L, W, H = self._dims()
        self._set_default_camera(L, W, H)
        self.render(preserve_camera=True)

    def _dims(self):
        items = (self.pallet or {}).get("packed_items", []) or []
        if items:
            dims = items[0].get("pallet_dims", {}) or {}
            L = safe_float(dims.get("length"), 0.0)
            W = safe_float(dims.get("width"), 0.0)
            H = safe_float(dims.get("height"), 0.0)
        else:
            L = W = H = 0.0
        if not L or not W:
            L = max([safe_float((it.get("position", {}) or {}).get("x"), 0) + safe_float(it.get("length"), 0) for it in items] or [1200.0])
            W = max([safe_float((it.get("position", {}) or {}).get("y"), 0) + safe_float(it.get("width"), 0) for it in items] or [1000.0])
        if not H:
            H = max([safe_float((it.get("position", {}) or {}).get("z"), 0) + safe_float(it.get("height"), 0) for it in items] or [720.0])
        return max(L, 1.0), max(W, 1.0), max(H, 1.0)

    def _add_line(self, pts, color=(0.25, 0.16, 0.08, 1.0), width=1.3):
        if not self.has_gl:
            return
        arr = self.np.array(pts, dtype=float)
        item = self.gl.GLLinePlotItem(pos=arr, color=color, width=width, antialias=True, mode="lines")
        self.view.addItem(item)
        self.scene_items.append(item)

    def _add_pallet_wireframe(self, L, W, H):
        corners = [
            [0, 0, 0], [L, 0, 0], [L, W, 0], [0, W, 0],
            [0, 0, H], [L, 0, H], [L, W, H], [0, W, H],
        ]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
        pts = []
        for a, b in edges:
            pts.append(corners[a])
            pts.append(corners[b])
        self._add_line(pts, width=2.0)

    def _box_meshdata(self):
        verts = self.np.array([
            [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
            [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
        ], dtype=float)
        faces = self.np.array([
            [0, 1, 2], [0, 2, 3],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [1, 2, 6], [1, 6, 5],
            [2, 3, 7], [2, 7, 6],
            [3, 0, 4], [3, 4, 7],
        ], dtype=int)
        return self.gl.MeshData(vertexes=verts, faces=faces)

    def _risk_level(self, item) -> int:
        # “吸盘矩形越界”在真实装箱执行里不作为箱体风险，
        # 这里过滤掉，避免风险箱数量、风险筛选和红黄框被它误触发。
        sr = safe_float(item.get("support_ratio"), safe_float(item.get("support_ratio_json"), 1.0))
        txt = normalize_risk_text(item.get("risk_text", ""))
        if sr < 0.70 or "严重" in txt or "超限" in txt or "箱体越界" in txt:
            return 2
        if sr < 0.90 or txt != "正常":
            return 1
        return 0

    def set_selected_box_key(self, key, render: bool = True) -> None:
        self.selected_box_key = key
        if render:
            self.render(preserve_camera=True)

    def _item_matches_selected(self, seq: int, item) -> bool:
        key = self.selected_box_key or {}
        if not key:
            return False
        try:
            if key.get("seq") is not None and int(key.get("seq")) == int(seq):
                return True
        except Exception:
            pass
        target_id = safe_str(key.get("box_id"), "")
        if target_id and target_id not in {"--", "-", "正常"}:
            candidates = [
                safe_str(item.get("id"), ""),
                safe_str(item.get("box_id"), ""),
                safe_str(item.get("original_box_id"), ""),
            ]
            if target_id in candidates:
                return True
        try:
            pos = item.get("position", {}) or {}
            checks = [
                (safe_float(pos.get("x"), float("nan")), safe_float(key.get("x"), float("nan"))),
                (safe_float(pos.get("y"), float("nan")), safe_float(key.get("y"), float("nan"))),
                (safe_float(pos.get("z"), float("nan")), safe_float(key.get("z"), float("nan"))),
                (safe_float(item.get("length"), float("nan")), safe_float(key.get("lx"), float("nan"))),
                (safe_float(item.get("width"), float("nan")), safe_float(key.get("ly"), float("nan"))),
                (safe_float(item.get("height"), float("nan")), safe_float(key.get("lz"), float("nan"))),
            ]
            return all((not math.isnan(a)) and (not math.isnan(b)) and abs(a - b) <= 1e-3 for a, b in checks)
        except Exception:
            return False

    def _color_for_item(self, idx, item, items):
        mode = self.color_mode
        if mode == "按重量着色":
            vals = [safe_float(it.get("weight"), 0.0) for it in items]
            vmin, vmax = min(vals or [0.0]), max(vals or [1.0])
            value = safe_float(item.get("weight"), 0.0)
            t = 0.0 if abs(vmax - vmin) < 1e-9 else (value - vmin) / (vmax - vmin)
            return blue_red_rgba(t, alpha=0.82)
        if mode == "按支撑风险着色":
            sr = safe_float(item.get("support_ratio"), 1.0)
            return blue_red_rgba(max(0.0, min(1.0, 1.0 - sr)), alpha=0.82)
        if mode == "按层高着色":
            pos = item.get("position", {}) or {}
            vals = [safe_float((it.get("position", {}) or {}).get("z"), 0.0) for it in items]
            vmin, vmax = min(vals or [0.0]), max(vals or [1.0])
            value = safe_float(pos.get("z"), 0.0)
            t = 0.0 if abs(vmax - vmin) < 1e-9 else (value - vmin) / (vmax - vmin)
            return blue_red_rgba(t, alpha=0.82)
        key = safe_str(item.get("type"), safe_str(item.get("suction_orientation"), str(idx)))
        return categorical_rgba(key, 0.82)

    def _add_box_outline(self, x, y, z, lx, ly, lz, color, width):
        x1, y1, z1 = x + lx, y + ly, z + lz
        pts = [
            [x, y, z], [x1, y, z], [x1, y, z], [x1, y1, z], [x1, y1, z], [x, y1, z], [x, y1, z], [x, y, z],
            [x, y, z1], [x1, y, z1], [x1, y, z1], [x1, y1, z1], [x1, y1, z1], [x, y1, z1], [x, y1, z1], [x, y, z1],
            [x, y, z], [x, y, z1], [x1, y, z], [x1, y, z1], [x1, y1, z], [x1, y1, z1], [x, y1, z], [x, y1, z1],
        ]
        self._add_line(pts, color=color, width=width)

    def _add_suction_rect(self, item, z_top):
        vals = [
            safe_float(item.get("suction_rect_x_min"), float("nan")),
            safe_float(item.get("suction_rect_x_max"), float("nan")),
            safe_float(item.get("suction_rect_y_min"), float("nan")),
            safe_float(item.get("suction_rect_y_max"), float("nan")),
        ]
        if any(math.isnan(v) for v in vals):
            return
        x0, x1, y0, y1 = vals
        z = z_top + 8.0
        pts = [[x0, y0, z], [x1, y0, z], [x1, y0, z], [x1, y1, z], [x1, y1, z], [x0, y1, z], [x0, y1, z], [x0, y0, z]]
        self._add_line(pts, color=(0.96, 0.45, 0.05, 0.95), width=1.8)

    def render(self, preserve_camera: bool = True):
        cam_state = self._camera_state() if (self.has_gl and preserve_camera) else None
        self._clear()
        if not self.has_gl:
            if isinstance(self.view, QtWidgets.QLabel):
                self.view.setText("缺少 3D 依赖" if self.pallet else "暂无托盘")
            return
        if not self.pallet:
            return

        all_items = list((self.pallet or {}).get("packed_items", []) or [])
        selected_item = None
        for original_seq, candidate in enumerate(all_items, start=1):
            if self._item_matches_selected(original_seq, candidate):
                selected_item = candidate
                break

        items = list(all_items)
        if self.only_risk:
            items = [it for it in items if self._risk_level(it) > 0]
        if self.visible_count is not None:
            items = items[:max(0, int(self.visible_count))]
        # 即使当前开启了“只看风险箱”或动画未播放到该箱，用户从表格点选的箱子也必须显示出来。
        if selected_item is not None and not any(it is selected_item for it in items):
            items.append(selected_item)
        L, W, H = self._dims()

        grid = self.gl.GLGridItem()
        grid.setSize(x=max(L, W) * 1.25, y=max(L, W) * 1.25, z=1)
        grid.setSpacing(x=max(L, W) / 6, y=max(L, W) / 6, z=1)
        grid.translate(L / 2, W / 2, -4)
        self.view.addItem(grid)
        self.scene_items.append(grid)
        self._add_pallet_wireframe(L, W, H)

        meshdata = self._box_meshdata()
        for idx, it in enumerate(items[:140]):
            pos = it.get("position", {}) or {}
            x = safe_float(pos.get("x"), 0.0)
            y = safe_float(pos.get("y"), 0.0)
            z = safe_float(pos.get("z"), 0.0)
            lx = max(safe_float(it.get("length"), 0.0), 1.0)
            ly = max(safe_float(it.get("width"), 0.0), 1.0)
            lz = max(safe_float(it.get("height"), 0.0), 1.0)
            rgba = self._color_for_item(idx, it, all_items)
            box = self.gl.GLMeshItem(
                meshdata=meshdata,
                smooth=False,
                color=rgba,
                shader="shaded",
                drawEdges=True,
                edgeColor=(0.10, 0.10, 0.10, 0.45),
                glOptions="translucent",
            )
            box.scale(lx, ly, lz)
            box.translate(x, y, z)
            self.view.addItem(box)
            self.scene_items.append(box)
            risk = self._risk_level(it)
            if risk > 0:
                color = (0.90, 0.05, 0.05, 1.0) if risk >= 2 else (0.95, 0.55, 0.05, 1.0)
                self._add_box_outline(x, y, z, lx, ly, lz, color, 2.8 if risk >= 2 else 2.0)
            if selected_item is not None and it is selected_item:
                # 选中箱体用高亮蓝色粗边框覆盖显示，方便从表格/风险列表定位到 3D 中的具体位置。
                self._add_box_outline(x, y, z, lx, ly, lz, (0.05, 0.35, 1.00, 1.0), 6.0)
                self._add_box_outline(x - 2, y - 2, z - 2, lx + 4, ly + 4, lz + 4, (1.00, 1.00, 1.00, 0.95), 2.0)
            if self.show_suction:
                self._add_suction_rect(it, z + lz)

        if cam_state:
            self._restore_camera_state(cam_state)
        else:
            self._set_default_camera(L, W, H)


class PalletPreviewCard(QtWidgets.QFrame):
    clicked = QtCore.pyqtSignal()
    request_zoom = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PalletPreviewCard")
        self.setProperty("selected", False)
        self.pallet = None
        self._anim_index = 0

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._anim_step)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel("--")
        self.title.setObjectName("PreviewTitle")
        self.stats = QtWidgets.QLabel("--")
        self.stats.setObjectName("PreviewSub")
        self.stats.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.btn_zoom = QtWidgets.QPushButton("⛶")
        self.btn_zoom.setObjectName("IconButton")
        self.btn_zoom.setFixedSize(24, 24)
        self.btn_zoom.setToolTip("在中间区域放大当前托盘")
        head.addWidget(self.title, 1)
        head.addWidget(self.stats)
        head.addWidget(self.btn_zoom)
        layout.addLayout(head)

        self.sub = QtWidgets.QLabel("--")
        self.sub.setObjectName("PreviewSub")
        layout.addWidget(self.sub)

        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(4)
        self.btn_final = QtWidgets.QPushButton("最终")
        self.btn_play = QtWidgets.QPushButton("播放")
        self.btn_pause = QtWidgets.QPushButton("暂停")
        self.btn_reset = QtWidgets.QPushButton("重置")
        for b in [self.btn_final, self.btn_play, self.btn_pause, self.btn_reset]:
            b.setObjectName("TinyButton")
            tools.addWidget(b)
        self.cmb_color = QtWidgets.QComboBox()
        self.cmb_color.addItems(["按支撑风险着色", "按重量着色", "按层高着色", "按箱型着色"])
        self.cmb_color.setMinimumWidth(110)
        tools.addWidget(self.cmb_color, 1)
        self.chk_suction = QtWidgets.QCheckBox("吸盘")
        self.chk_suction.setChecked(True)
        self.chk_risk = QtWidgets.QCheckBox("风险")
        tools.addWidget(self.chk_suction)
        tools.addWidget(self.chk_risk)
        layout.addLayout(tools)

        self.canvas = PalletPreviewCanvas()
        self.canvas.clicked.connect(self.clicked)
        layout.addWidget(self.canvas, 1)

        self.btn_final.clicked.connect(self.show_final)
        self.btn_play.clicked.connect(self.play)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_reset.clicked.connect(self.reset)
        self.btn_zoom.clicked.connect(lambda: self.request_zoom.emit(self.pallet))
        self.cmb_color.currentIndexChanged.connect(self._apply_options)
        self.chk_suction.stateChanged.connect(self._apply_options)
        self.chk_risk.stateChanged.connect(self._apply_options)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def set_selected(self, selected: bool):
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_data(self, pallet: Optional[dict], selected: bool = False, selected_box_key=None):
        self.timer.stop()
        self.pallet = pallet
        self._anim_index = 0
        self.set_selected(selected)
        self.canvas.set_selected_box_key(selected_box_key if selected else None, render=False)
        if not pallet:
            self.title.setText("空位")
            self.sub.setText("")
            self.stats.setText("")
            self.canvas.set_pallet(None)
            return
        pid = safe_str(pallet.get("pallet_id"), "--")
        status = safe_str(pallet.get("mpm_status"), "UNKNOWN")
        count = len(pallet.get("packed_items", []) or [])
        fill_rate = safe_float(pallet.get("fill_rate"), float("nan"))
        fill_txt = "--" if math.isnan(fill_rate) else f"{fill_rate * 100:.1f}%"
        mpm_total = safe_float(pallet.get("mpm_total"), float("nan"))
        mpm_target = safe_float(pallet.get("mpm_target"), float("nan"))
        if not math.isnan(mpm_total) and not math.isnan(mpm_target) and mpm_target > 0:
            index_txt = f"{mpm_total:g}/{mpm_target:g}"
        else:
            index_txt = status
        self.title.setText(pid)
        self.sub.setText(f"状态：{status}｜箱数：{count}｜填充率：{fill_txt}｜指数：{index_txt}")
        self.stats.setText("当前选中" if selected else "点击选择")
        self.canvas.set_pallet(pallet)
        self._apply_options()

    def _apply_options(self):
        self.canvas.set_options(
            show_suction=self.chk_suction.isChecked(),
            only_risk=self.chk_risk.isChecked(),
            color_mode=self.cmb_color.currentText(),
            visible_count=None if self._anim_index <= 0 else self._anim_index,
        )

    def show_final(self):
        if not self.pallet:
            return
        self.timer.stop()
        self._anim_index = len(self.pallet.get("packed_items", []) or [])
        self.canvas.set_options(visible_count=None)

    def play(self):
        if not self.pallet:
            return
        self._anim_index = 0
        self.timer.start(400)

    def pause(self):
        self.timer.stop()

    def reset(self):
        self.timer.stop()
        self._anim_index = 0
        self.canvas.set_options(visible_count=0, reset_camera=True)

    def _anim_step(self):
        if not self.pallet:
            self.timer.stop()
            return
        n = len(self.pallet.get("packed_items", []) or [])
        if self._anim_index >= n:
            self.timer.stop()
            self.canvas.set_options(visible_count=None)
            return
        self._anim_index += max(1, n // 40)
        self.canvas.set_options(visible_count=min(self._anim_index, n))


class IndustrialPackingWorkbench(BaseDashboard):
    """A friendlier industrial UI that reuses the stable calculation and 3D logic."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir).resolve()
        self.config_path = self.project_dir / DEFAULT_CONFIG_REL
        self.worker: Optional[PackingWorker] = None
        self.overview_page = 0
        self.overview_cards = []
        ensure_runtime_dirs(self.project_dir)
        super().__init__()
        self.setWindowTitle("工业装箱工作台 V2 - 后端装箱 + 前端可视化")
        self.resize(1840, 1060)
        self._set_status("idle")
        self._write_log(f"[UI] 工作区目录：{workspace_dir_from_project(self.project_dir)}")
        self._write_log(f"[UI] 算法源码目录：{self.project_dir}")
        self._write_log(f"[UI] 默认配置：{self.config_path}")
        self._write_log(f"[UI] 当前 Python：{sys.executable}")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        body.setObjectName("BodySplitter")
        body.setChildrenCollapsible(False)
        body.addWidget(self._build_left_workflow())
        body.addWidget(self._build_center_workspace())
        body.addWidget(self._build_right_summary())
        body.setSizes([350, 1030, 410])
        root.addWidget(body, 1)

        root.addWidget(self._build_bottom_log())

    def _build_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QFrame()
        header.setObjectName("Header")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(12)

        title_box = QtWidgets.QVBoxLayout()
        self.title_label = QtWidgets.QLabel("工业装箱工作台")
        self.title_label.setObjectName("MainTitle")
        self.subtitle_label = QtWidgets.QLabel("装箱算法后端计算 · 托盘可视化 · 稳定性评估 · 风险提示")
        self.subtitle_label.setObjectName("MainSubtitle")
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        layout.addLayout(title_box, 1)

        self.status_pill = StatusPill("空闲")
        layout.addWidget(self.status_pill)

        self.btn_choose_project = QtWidgets.QPushButton("算法目录")
        self.btn_choose_project.setObjectName("GhostButton")
        self.btn_choose_project.clicked.connect(self.choose_project_dir)
        layout.addWidget(self.btn_choose_project)

        self.btn_choose_config = QtWidgets.QPushButton("配置文件")
        self.btn_choose_config.setObjectName("GhostButton")
        self.btn_choose_config.clicked.connect(self.choose_config_file)
        layout.addWidget(self.btn_choose_config)

        self.btn_start_backend = QtWidgets.QPushButton("开始装箱")
        self.btn_start_backend.setObjectName("PrimaryButton")
        self.btn_start_backend.clicked.connect(self.start_backend_packing)
        layout.addWidget(self.btn_start_backend)

        self.btn_stop_backend = QtWidgets.QPushButton("停止")
        self.btn_stop_backend.setObjectName("DangerButton")
        self.btn_stop_backend.clicked.connect(self.stop_backend_packing)
        self.btn_stop_backend.setEnabled(False)
        layout.addWidget(self.btn_stop_backend)

        self.btn_load_result = QtWidgets.QPushButton("打开结果文件")
        self.btn_load_result.setObjectName("GhostButton")
        self.btn_load_result.clicked.connect(self.load_json_dialog)
        layout.addWidget(self.btn_load_result)

        self.btn_open_latest = QtWidgets.QPushButton("打开最新结果")
        self.btn_open_latest.setObjectName("GhostButton")
        self.btn_open_latest.clicked.connect(self.open_latest_result)
        layout.addWidget(self.btn_open_latest)
        return header

    def _build_left_workflow(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("LeftPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 10, 14)
        layout.setSpacing(10)

        self.file_info = QtWidgets.QLabel("尚未加载结果文件")
        self.file_info.setObjectName("SmallInfo")
        self.file_info.setWordWrap(True)
        layout.addWidget(self.file_info)

        self.step_run = StepCard("1", "运行状态", "等待开始")
        layout.addWidget(self.step_run)
        self.run_progress = QtWidgets.QProgressBar()
        self.run_progress.setRange(0, 100)
        self.run_progress.setValue(0)
        self.run_progress.setTextVisible(True)
        self.run_progress.setFormat("等待开始")
        layout.addWidget(self.run_progress)

        self.step_result = StepCard("2", "筛选托盘", "中间区域每页显示 6 个三维托盘")
        layout.addWidget(self.step_result)
        filter_box = QtWidgets.QFrame()
        filter_box.setObjectName("ParamBox")
        f = QtWidgets.QFormLayout(filter_box)
        f.setContentsMargins(12, 10, 12, 10)
        f.setSpacing(8)
        self.cmb_status = QtWidgets.QComboBox()
        self.cmb_status.addItems(["全部", "SUCCESS", "FAILED", "UNKNOWN"])
        self.cmb_status.currentIndexChanged.connect(self.refresh_pallet_filter)
        self.cmb_order = QtWidgets.QComboBox()
        self.cmb_order.addItem("全部订单")
        self.cmb_order.currentIndexChanged.connect(self.refresh_pallet_filter)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("托盘ID / 箱型 / 订单号")
        self.search_edit.textChanged.connect(self.refresh_pallet_filter)
        self.cmb_pallet = QtWidgets.QComboBox()
        self.cmb_pallet.currentIndexChanged.connect(self.on_pallet_changed)
        self.cmb_pallet.setVisible(False)
        self.lbl_filter_count = QtWidgets.QLabel("可选托盘：0")
        self.lbl_filter_count.setObjectName("SmallInfo")
        self.btn_left_prev_page = QtWidgets.QPushButton("上一页")
        self.btn_left_prev_page.setObjectName("MiniButton")
        self.btn_left_prev_page.clicked.connect(self._prev_overview_page)
        self.btn_left_next_page = QtWidgets.QPushButton("下一页")
        self.btn_left_next_page.setObjectName("MiniButton")
        self.btn_left_next_page.clicked.connect(self._next_overview_page)
        nav = QtWidgets.QHBoxLayout()
        nav.addWidget(self.btn_left_prev_page)
        nav.addWidget(self.btn_left_next_page)
        nav.addStretch(1)
        self.lbl_left_page = QtWidgets.QLabel("第 1 / 1 页")
        self.lbl_left_page.setObjectName("SmallInfo")
        nav.addWidget(self.lbl_left_page)
        f.addRow("状态", self.cmb_status)
        f.addRow("订单", self.cmb_order)
        f.addRow("搜索", self.search_edit)
        f.addRow("分页", nav)
        f.addRow("结果", self.lbl_filter_count)
        layout.addWidget(filter_box)

        overview_box = QtWidgets.QFrame()
        overview_box.setObjectName("OverviewBox")
        grid = QtWidgets.QGridLayout(overview_box)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(8)
        self.card_total = MetricCard("总托盘数")
        self.card_success = MetricCard("成功托盘")
        self.card_failed = MetricCard("失败托盘")
        self.card_gap = MetricCard("平均指数缺口")
        self.card_avg_fill = MetricCard("平均填充率")
        grid.addWidget(self.card_total, 0, 0)
        grid.addWidget(self.card_success, 0, 1)
        grid.addWidget(self.card_failed, 1, 0)
        grid.addWidget(self.card_gap, 1, 1)
        grid.addWidget(self.card_avg_fill, 2, 0, 1, 2)
        layout.addWidget(overview_box)

        self.step_params = StepCard("3", "高级参数", "默认收起，不建议频繁修改")
        layout.addWidget(self.step_params)
        self.btn_toggle_params = QtWidgets.QToolButton()
        self.btn_toggle_params.setText("展开高级参数")
        self.btn_toggle_params.setCheckable(True)
        self.btn_toggle_params.setArrowType(QtCore.Qt.RightArrow)
        self.btn_toggle_params.clicked.connect(self._toggle_param_panel)
        layout.addWidget(self.btn_toggle_params)

        self.param_box = QtWidgets.QFrame()
        self.param_box.setObjectName("ParamBox")
        form = QtWidgets.QFormLayout(self.param_box)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        self._applying_param_scheme = False
        self.cmb_param_scheme = QtWidgets.QComboBox()
        self.cmb_param_scheme.addItems(list(PARAMETER_SCHEMES.keys()) + ["自定义"])
        self.cmb_param_scheme.currentTextChanged.connect(self.apply_parameter_scheme)
        self.lbl_param_scheme_desc = QtWidgets.QLabel(PARAMETER_SCHEMES["标准方案"]["desc"])
        self.lbl_param_scheme_desc.setObjectName("SmallInfo")
        self.lbl_param_scheme_desc.setWordWrap(True)
        self.sp_z_tol = QtWidgets.QDoubleSpinBox()
        self.sp_z_tol.setRange(0.0, 50.0)
        self.sp_z_tol.setValue(5.0)
        self.sp_z_tol.setSuffix(" mm")
        self.sp_z_tol.setDecimals(1)
        self.sp_ax = QtWidgets.QDoubleSpinBox()
        self.sp_ax.setRange(0.01, 2.0)
        self.sp_ax.setValue(0.40)
        self.sp_ax.setDecimals(2)
        self.sp_ay = QtWidgets.QDoubleSpinBox()
        self.sp_ay.setRange(0.01, 2.0)
        self.sp_ay.setValue(0.30)
        self.sp_ay.setDecimals(2)
        self.sp_mu = QtWidgets.QDoubleSpinBox()
        self.sp_mu.setRange(0.01, 2.0)
        self.sp_mu.setValue(0.45)
        self.sp_mu.setDecimals(2)
        for sp in [self.sp_z_tol, self.sp_ax, self.sp_ay, self.sp_mu]:
            sp.valueChanged.connect(self.mark_parameter_custom)
        form.addRow("参数方案", self.cmb_param_scheme)
        form.addRow("说明", self.lbl_param_scheme_desc)
        form.addRow("接触Z容差", self.sp_z_tol)
        form.addRow("X向加速度/g", self.sp_ax)
        form.addRow("Y向加速度/g", self.sp_ay)
        form.addRow("摩擦系数", self.sp_mu)
        self.param_box.setVisible(False)
        layout.addWidget(self.param_box)
        layout.addStretch(1)
        return panel

    def _build_center_workspace(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("CenterPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(8, 14, 8, 10)
        layout.setSpacing(10)

        self.workspace_tabs = QtWidgets.QTabWidget()
        self.workspace_tabs.setObjectName("WorkspaceTabs")
        self.workspace_tabs.addTab(self._build_visual_tab(), "3D装箱视图")
        self.workspace_tabs.addTab(self._build_table_tab(), "箱子列表")
        self.workspace_tabs.addTab(self._build_failed_tab(), "失败列表")
        self.workspace_tabs.addTab(self._build_analysis_tab(), "稳定性分析")
        layout.addWidget(self.workspace_tabs, 1)
        return panel

    def _build_visual_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        overview_box = QtWidgets.QFrame()
        overview_box.setObjectName("VisualFrame")
        ov_layout = QtWidgets.QVBoxLayout(overview_box)
        ov_layout.setContentsMargins(10, 10, 10, 10)
        ov_layout.setSpacing(8)

        ov_top = QtWidgets.QHBoxLayout()
        ov_title = QtWidgets.QLabel("六托盘三维总览（每页 6 个，可拖动/缩放/旋转，点击托盘联动右侧数据）")
        ov_title.setObjectName("SectionTitle")
        ov_top.addWidget(ov_title)
        ov_top.addStretch(1)
        self.btn_prev_page = QtWidgets.QPushButton("上一页")
        self.btn_prev_page.setObjectName("MiniButton")
        self.btn_prev_page.clicked.connect(self._prev_overview_page)
        self.btn_next_page = QtWidgets.QPushButton("下一页")
        self.btn_next_page.setObjectName("MiniButton")
        self.btn_next_page.clicked.connect(self._next_overview_page)
        self.lbl_overview_page = QtWidgets.QLabel("第 1 / 1 页")
        self.lbl_overview_page.setObjectName("SmallInfo")
        ov_top.addWidget(self.btn_prev_page)
        ov_top.addWidget(self.btn_next_page)
        ov_top.addWidget(self.lbl_overview_page)
        ov_layout.addLayout(ov_top)

        self.overview_grid = QtWidgets.QGridLayout()
        self.overview_grid.setSpacing(10)
        self.overview_cards = []
        for i in range(6):
            card = PalletPreviewCard()
            card.clicked.connect(lambda idx=i: self._on_overview_card_clicked(idx))
            card.request_zoom.connect(self._enter_pallet_zoom)
            self.overview_cards.append(card)
            self.overview_grid.addWidget(card, i // 3, i % 3)
        ov_layout.addLayout(self.overview_grid, 1)

        self.overview_box = overview_box
        self.zoom_box = self._build_center_zoom_panel()
        self.zoom_box.setVisible(False)
        layout.addWidget(self.overview_box, 1)
        layout.addWidget(self.zoom_box, 1)
        return tab

    def _build_center_zoom_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QFrame()
        panel.setObjectName("VisualFrame")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QtWidgets.QFrame()
        header.setObjectName("ZoomHeader")
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(12, 10, 12, 10)
        self.zoom_title = QtWidgets.QLabel("--")
        self.zoom_title.setObjectName("ZoomTitle")
        self.zoom_sub = QtWidgets.QLabel("请选择一个托盘")
        self.zoom_sub.setObjectName("ZoomSub")
        self.btn_zoom_back = QtWidgets.QPushButton("返回总览")
        self.btn_zoom_back.setObjectName("GhostButton")
        self.btn_zoom_back.clicked.connect(self._exit_pallet_zoom)
        h.addWidget(self.zoom_title)
        h.addWidget(self.zoom_sub, 1)
        h.addWidget(self.btn_zoom_back)
        layout.addWidget(header)

        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(6)
        self.zoom_btn_final = QtWidgets.QPushButton("最终")
        self.zoom_btn_play = QtWidgets.QPushButton("播放")
        self.zoom_btn_pause = QtWidgets.QPushButton("暂停")
        self.zoom_btn_reset = QtWidgets.QPushButton("重置")
        for b in [self.zoom_btn_final, self.zoom_btn_play, self.zoom_btn_pause, self.zoom_btn_reset]:
            b.setObjectName("MiniButton")
            tools.addWidget(b)
        tools.addWidget(QtWidgets.QLabel("着色"))
        self.zoom_cmb_color = QtWidgets.QComboBox()
        self.zoom_cmb_color.addItems(["按支撑风险着色", "按重量着色", "按层高着色", "按箱型着色"])
        self.zoom_cmb_color.setMinimumWidth(180)
        tools.addWidget(self.zoom_cmb_color)
        self.zoom_chk_suction = QtWidgets.QCheckBox("吸盘")
        self.zoom_chk_suction.setChecked(True)
        self.zoom_chk_risk = QtWidgets.QCheckBox("风险箱")
        tools.addWidget(self.zoom_chk_suction)
        tools.addWidget(self.zoom_chk_risk)
        tools.addStretch(1)
        layout.addLayout(tools)

        self.zoom_canvas = PalletPreviewCanvas()
        self.zoom_canvas.setMinimumHeight(560)
        layout.addWidget(self.zoom_canvas, 1)

        self.zoom_timer = QtCore.QTimer(self)
        self.zoom_timer.timeout.connect(self._zoom_anim_step)
        self.zoom_pallet = None
        self.zoom_anim_index = 0

        self.zoom_btn_final.clicked.connect(self._zoom_show_final)
        self.zoom_btn_play.clicked.connect(self._zoom_play)
        self.zoom_btn_pause.clicked.connect(self._zoom_pause)
        self.zoom_btn_reset.clicked.connect(self._zoom_reset)
        self.zoom_cmb_color.currentIndexChanged.connect(self._apply_zoom_options)
        self.zoom_chk_suction.stateChanged.connect(self._apply_zoom_options)
        self.zoom_chk_risk.stateChanged.connect(self._apply_zoom_options)
        return panel

    @staticmethod
    def _has_gl() -> bool:
        try:
            import pyqtgraph.opengl  # noqa: F401
            return True
        except Exception:
            return False

    def _build_table_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        hint = QtWidgets.QLabel("默认显示关键字段。点击某一行后，右侧会显示箱子详细信息并在 3D 视图中高亮。")
        hint.setObjectName("HintLabel")
        layout.addWidget(hint)
        self.box_table = QtWidgets.QTableWidget()
        self.box_table.setColumnCount(18)
        self.box_table.setHorizontalHeaderLabels([
            "序号", "箱号", "类型", "长×宽×高(mm)", "重量(kg)", "X", "Y", "Z", "层号",
            "支撑率", "支撑面积", "承压利用", "吸附箱角", "吸盘角", "吸盘规格", "吸附矩形", "MPM", "风险",
        ])
        self.box_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.box_table.horizontalHeader().setStretchLastSection(True)
        self.box_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.box_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.box_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.box_table.setAlternatingRowColors(True)
        self.box_table.itemSelectionChanged.connect(self.on_table_selection_changed)
        layout.addWidget(self.box_table, 1)
        return tab

    def _build_failed_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        hint = QtWidgets.QLabel("这里优先显示算法输出中的失败箱；如果当前 JSON 没有逐箱失败字段，则显示 FAILED 托盘、MPM 缺口和低填充率原因。双击行可跳转到对应托盘。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.failed_table = QtWidgets.QTableWidget()
        self.failed_table.setColumnCount(10)
        self.failed_table.setHorizontalHeaderLabels(["类型", "托盘", "订单", "状态", "箱号", "箱型/托盘型", "数量", "缺口", "填充率", "原因"])
        self.failed_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.failed_table.horizontalHeader().setStretchLastSection(True)
        self.failed_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.failed_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.failed_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.failed_table.setAlternatingRowColors(True)
        self.failed_table.cellDoubleClicked.connect(self.on_failed_table_double_clicked)
        self.failed_table.itemSelectionChanged.connect(self.on_failed_table_selection_changed)
        layout.addWidget(self.failed_table, 1)
        return tab

    def _build_analysis_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        top = QtWidgets.QGridLayout()
        self.kpi_score_big = SummaryKpi("综合评分", "--", "等待结果")
        self.kpi_level_big = SummaryKpi("稳定等级", "--", "等待结果")
        self.kpi_support_big = SummaryKpi("平均支撑率", "--", "等待结果")
        self.kpi_risk_big = SummaryKpi("风险箱数量", "--", "等待结果")
        top.addWidget(self.kpi_score_big, 0, 0)
        top.addWidget(self.kpi_level_big, 0, 1)
        top.addWidget(self.kpi_support_big, 0, 2)
        top.addWidget(self.kpi_risk_big, 0, 3)
        layout.addLayout(top)

        self.score_table = QtWidgets.QTableWidget()
        self.score_table.setColumnCount(5)
        self.score_table.setHorizontalHeaderLabels(["指标", "分数", "当前值", "状态", "说明"])
        self.score_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.score_table.horizontalHeader().setStretchLastSection(True)
        self.score_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.score_table.setAlternatingRowColors(True)
        layout.addWidget(self.score_table, 1)
        return tab

    def _build_right_summary(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("RightPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(10, 14, 14, 10)
        layout.setSpacing(10)

        pallet_card = QtWidgets.QFrame()
        pallet_card.setObjectName("PalletHero")
        p_layout = QtWidgets.QVBoxLayout(pallet_card)
        p_layout.setContentsMargins(14, 12, 14, 12)
        self.lbl_pallet_title = QtWidgets.QLabel("当前托盘")
        self.lbl_pallet_title.setObjectName("HeroTitle")
        self.lbl_pallet_id = QtWidgets.QLabel("--")
        self.lbl_pallet_id.setObjectName("HeroValue")
        self.lbl_pallet_sub = QtWidgets.QLabel("请先开始装箱或打开结果")
        self.lbl_pallet_sub.setObjectName("HeroSub")
        p_layout.addWidget(self.lbl_pallet_title)
        p_layout.addWidget(self.lbl_pallet_id)
        p_layout.addWidget(self.lbl_pallet_sub)
        layout.addWidget(pallet_card)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        self.card_score = MetricCard("综合评分")
        self.card_level = MetricCard("稳定等级")
        self.card_boxes = MetricCard("箱子数量")
        self.card_mass = MetricCard("总重量")
        self.card_fill = MetricCard("填充率")
        self.card_mpm = MetricCard("指数")
        self.card_height = MetricCard("高度利用率")
        self.card_support = MetricCard("平均支撑率")
        self.card_cg = MetricCard("重心偏移")
        cards = [
            self.card_fill, self.card_mpm, self.card_score, self.card_level,
            self.card_boxes, self.card_mass, self.card_height, self.card_support, self.card_cg,
        ]
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 2, i % 2)
        layout.addLayout(grid)

        suggestion_box = QtWidgets.QFrame()
        suggestion_box.setObjectName("SuggestionBox")
        s_layout = QtWidgets.QVBoxLayout(suggestion_box)
        s_layout.setContentsMargins(12, 10, 12, 10)
        title = QtWidgets.QLabel("操作建议")
        title.setObjectName("SectionTitle")
        self.suggestion_label = QtWidgets.QLabel("等待装箱结果。完成后这里会用人话解释主要风险。")
        self.suggestion_label.setObjectName("SuggestionText")
        self.suggestion_label.setWordWrap(True)
        s_layout.addWidget(title)
        s_layout.addWidget(self.suggestion_label)
        layout.addWidget(suggestion_box)

        risk_box = QtWidgets.QFrame()
        risk_box.setObjectName("RiskBox")
        r_layout = QtWidgets.QVBoxLayout(risk_box)
        r_layout.setContentsMargins(12, 10, 12, 10)
        r_title = QtWidgets.QLabel("风险与异常")
        r_title.setObjectName("SectionTitle")
        self.warning_list = QtWidgets.QListWidget()
        self.warning_list.setObjectName("WarningList")
        self.warning_list.setMinimumHeight(110)
        self.warning_list.itemClicked.connect(self.on_warning_item_clicked)
        r_layout.addWidget(r_title)
        r_layout.addWidget(self.warning_list, 1)
        layout.addWidget(risk_box, 1)

        detail_box = QtWidgets.QFrame()
        detail_box.setObjectName("DetailBox")
        d_layout = QtWidgets.QVBoxLayout(detail_box)
        d_layout.setContentsMargins(12, 10, 12, 10)
        d_title = QtWidgets.QLabel("箱子详细信息")
        d_title.setObjectName("SectionTitle")
        self.box_detail = QtWidgets.QPlainTextEdit()
        self.box_detail.setReadOnly(True)
        self.box_detail.setMinimumHeight(85)
        self.box_detail.setMaximumHeight(120)
        self.box_detail.setPlainText("在箱子列表中选择一行后显示详细信息。")
        detail_box.setMaximumHeight(175)
        d_layout.addWidget(d_title)
        d_layout.addWidget(self.box_detail, 0)
        layout.addWidget(detail_box, 0)
        return panel

    def _build_bottom_log(self) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setObjectName("BottomLog")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("运行消息")
        lbl.setObjectName("SectionTitle")
        top.addWidget(lbl)
        top.addStretch(1)
        self.btn_toggle_log = QtWidgets.QPushButton("展开日志")
        self.btn_toggle_log.setObjectName("MiniButton")
        self.btn_toggle_log.setCheckable(True)
        self.btn_toggle_log.clicked.connect(self.toggle_bottom_log)
        top.addWidget(self.btn_toggle_log)
        self.btn_clear_log = QtWidgets.QPushButton("清空")
        self.btn_clear_log.setObjectName("MiniButton")
        self.btn_clear_log.clicked.connect(lambda: self.log_box.clear())
        top.addWidget(self.btn_clear_log)
        layout.addLayout(top)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setObjectName("LogBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(4000)
        self.log_box.setFixedHeight(115)
        self.log_box.setVisible(False)
        layout.addWidget(self.log_box)
        return box

    def toggle_bottom_log(self) -> None:
        checked = bool(self.btn_toggle_log.isChecked()) if hasattr(self, "btn_toggle_log") else True
        if hasattr(self, "log_box"):
            self.log_box.setVisible(checked)
        if hasattr(self, "btn_toggle_log"):
            self.btn_toggle_log.setText("收起日志" if checked else "展开日志")

    def _apply_style(self) -> None:
        self.setStyleSheet(r"""
        * {
            font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", Arial;
            font-size: 14px;
            color: #111827;
        }
        QWidget#Root, QWidget#LeftPanel, QWidget#CenterPanel, QWidget#RightPanel {
            background: #F3F6FA;
        }
        QFrame#Header {
            background: #111827;
            border-bottom: 1px solid #1F2937;
        }
        QLabel#MainTitle {
            color: #FFFFFF;
            font-size: 28px;
            font-weight: 900;
        }
        QLabel#MainSubtitle {
            color: #CBD5E1;
            font-size: 12px;
        }
        QLabel#StatusPill {
            color: #FFFFFF;
            border-radius: 14px;
            padding: 6px 12px;
            font-weight: 800;
        }
        QLabel#StatusPill[state="idle"] { background: #64748B; }
        QLabel#StatusPill[state="running"] { background: #2563EB; }
        QLabel#StatusPill[state="done"] { background: #16A34A; }
        QLabel#StatusPill[state="error"] { background: #DC2626; }
        QLabel#StatusPill[state="stopped"] { background: #F59E0B; }

        QPushButton {
            border: none;
            border-radius: 8px;
            padding: 10px 16px;
            font-weight: 800;
            background: #E5E7EB;
        }
        QPushButton:hover { background: #D1D5DB; }
        QPushButton:disabled { background: #CBD5E1; color: #64748B; }
        QPushButton#PrimaryButton { background: #2563EB; color: white; }
        QPushButton#PrimaryButton:hover { background: #1D4ED8; }
        QPushButton#DangerButton { background: #DC2626; color: white; }
        QPushButton#DangerButton:hover { background: #B91C1C; }
        QPushButton#GhostButton { background: #263244; color: #E5E7EB; border: 1px solid #334155; }
        QPushButton#GhostButton:hover { background: #334155; }
        QPushButton#MiniButton { background: #E5E7EB; padding: 6px 10px; }
        QPushButton#IconButton {
            background: #E5E7EB;
            color: #334155;
            border-radius: 6px;
            padding: 0px;
            font-weight: 900;
        }
        QPushButton#IconButton:hover { background: #CBD5E1; }

        QPushButton#TinyButton {
            background: #E5E7EB;
            padding: 4px 7px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 800;
        }
        QPushButton#TinyButton:hover { background: #D1D5DB; }


        QFrame#StepCard, QFrame#ParamBox, QFrame#OverviewBox, QFrame#PalletHero,
        QFrame#SuggestionBox, QFrame#RiskBox, QFrame#DetailBox, QFrame#BottomLog,
        QFrame#VisualFrame {
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 12px;
        }
        QFrame#StepCard[state="active"] { border: 1px solid #93C5FD; background: #EFF6FF; }
        QFrame#StepCard[state="done"] { border: 1px solid #86EFAC; background: #F0FDF4; }
        QFrame#StepCard[state="error"] { border: 1px solid #FCA5A5; background: #FEF2F2; }
        QLabel#StepBadge {
            background: #2563EB;
            color: #FFFFFF;
            border-radius: 13px;
            font-weight: 900;
        }
        QLabel#StepTitle, QLabel#SectionTitle {
            color: #7F1D1D;
            font-weight: 900;
        }
        QLabel#StepDesc, QLabel#SmallInfo, QLabel#HintLabel, QLabel#HeroSub, QLabel#SuggestionText {
            color: #64748B;
            line-height: 145%;
        }
        QLabel#HeroTitle { color: #64748B; font-weight: 800; }
        QLabel#HeroValue { color: #111827; font-size: 28px; font-weight: 900; }
        QLabel#AnimLabel { color: #475569; font-weight: 800; }

        QTabWidget::pane {
            border: 1px solid #E5E7EB;
            background: #FFFFFF;
            border-radius: 12px;
        }
        QTabBar::tab {
            background: #E5E7EB;
            color: #475569;
            padding: 10px 18px;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            margin-right: 2px;
            font-weight: 800;
        }
        QTabBar::tab:selected {
            background: #FFFFFF;
            color: #1D4ED8;
            border: 1px solid #E5E7EB;
            border-bottom: none;
        }
        QTableWidget, QPlainTextEdit, QListWidget, QComboBox, QLineEdit, QDoubleSpinBox {
            background: #FFFFFF;
            border: 1px solid #CBD5E1;
            border-radius: 8px;
            padding: 5px;
        }
        QPlainTextEdit#LogBox {
            background: #0F172A;
            color: #D1E7FF;
            font-family: Consolas, "Microsoft YaHei UI";
            border: 1px solid #1E293B;
        }
        QHeaderView::section {
            background: #DBEAFE;
            color: #1E3A8A;
            font-weight: 900;
            padding: 7px;
            border: 1px solid #BFDBFE;
        }
        QTableWidget {
            alternate-background-color: #F8FAFC;
            selection-background-color: #DBEAFE;
            selection-color: #111827;
            gridline-color: #E5E7EB;
        }
        QListWidget#WarningList::item { padding: 6px; }
        QProgressBar {
            border: 1px solid #CBD5E1;
            border-radius: 8px;
            text-align: center;
            background: #FFFFFF;
            height: 22px;
            font-weight: 800;
        }
        QProgressBar::chunk {
            background: #2563EB;
            border-radius: 7px;
        }
        QFrame#MetricCard, QFrame#SummaryKpi {
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 10px;
        }
        QFrame#MetricCard[state="good"], QFrame#SummaryKpi[state="good"] {
            border: 1px solid #86EFAC;
            background: #F0FDF4;
        }
        QFrame#MetricCard[state="warn"], QFrame#SummaryKpi[state="warn"] {
            border: 1px solid #FDE68A;
            background: #FFFBEB;
        }
        QFrame#MetricCard[state="bad"], QFrame#SummaryKpi[state="bad"] {
            border: 1px solid #FCA5A5;
            background: #FEF2F2;
        }
        QLabel#MetricTitle, QLabel#KpiTitle { color: #64748B; font-weight: 800; font-size: 12px; }
        QLabel#MetricValue, QLabel#KpiValue { color: #111827; font-weight: 900; font-size: 24px; }
        QLabel#MetricSub, QLabel#KpiUnit { color: #64748B; font-size: 12px; }
        QFrame#PalletPreviewCard {
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 12px;
        }
        QFrame#PalletPreviewCard[selected="true"] {
            border: 2px solid #2563EB;
            background: #EFF6FF;
        }
        QLabel#PreviewTitle { color: #0F172A; font-weight: 900; font-size: 15px; }
        QLabel#PreviewSub { color: #64748B; font-size: 12px; }
        QFrame#PalletPreviewCard[selected="true"] QLabel#PreviewSub { color: #1D4ED8; font-weight: 800; }
        QFrame#ZoomHeader {
            background: #F8FAFC;
            border: 1px solid #E5E7EB;
            border-radius: 12px;
        }
        QLabel#ZoomTitle { color: #0F172A; font-size: 22px; font-weight: 900; }
        QLabel#ZoomSub { color: #475569; font-size: 14px; font-weight: 800; }
        QSplitter::handle { background: #D8DEE9; }
        """)

    # ------------------------------------------------------------- parameter schemes / pallet switch
    def apply_parameter_scheme(self, name: str) -> None:
        """Apply a named stability-evaluation preset.

        这里不修改后端装箱算法配置，只修改 UI 中用于稳定性复核的 4 个参数。
        """
        if name not in PARAMETER_SCHEMES:
            if hasattr(self, "lbl_param_scheme_desc"):
                self.lbl_param_scheme_desc.setText("手动修改后的参数。")
            return
        scheme = PARAMETER_SCHEMES[name]
        self._applying_param_scheme = True
        try:
            self.sp_z_tol.setValue(float(scheme["z_tol"]))
            self.sp_ax.setValue(float(scheme["ax"]))
            self.sp_ay.setValue(float(scheme["ay"]))
            self.sp_mu.setValue(float(scheme["mu"]))
        finally:
            self._applying_param_scheme = False
        if hasattr(self, "lbl_param_scheme_desc"):
            self.lbl_param_scheme_desc.setText(str(scheme.get("desc", "")))
        if getattr(self, "current_pallet", None) is not None:
            self.calculate_current(show_message=False)
            self._write_log(f"[UI] 已应用参数方案：{name}，并刷新当前托盘评价。")

    def mark_parameter_custom(self) -> None:
        if getattr(self, "_applying_param_scheme", False):
            return
        if not hasattr(self, "cmb_param_scheme"):
            return
        if self.cmb_param_scheme.currentText() != "自定义":
            self.cmb_param_scheme.blockSignals(True)
            self.cmb_param_scheme.setCurrentText("自定义")
            self.cmb_param_scheme.blockSignals(False)
        if hasattr(self, "lbl_param_scheme_desc"):
            self.lbl_param_scheme_desc.setText("手动修改后的参数。")
        if getattr(self, "current_pallet", None) is not None:
            self.calculate_current(show_message=False)

    def _sync_visual_pallet_combo(self) -> None:
        if not hasattr(self, "cmb_visual_pallet") or not hasattr(self, "cmb_pallet"):
            return
        self.cmb_visual_pallet.blockSignals(True)
        try:
            self.cmb_visual_pallet.clear()
            for i in range(self.cmb_pallet.count()):
                self.cmb_visual_pallet.addItem(self.cmb_pallet.itemText(i))
            self.cmb_visual_pallet.setCurrentIndex(self.cmb_pallet.currentIndex())
        finally:
            self.cmb_visual_pallet.blockSignals(False)


    def build_box_risk_text(self, row, L: float, W: float, H: float) -> str:
        """业务版风险判断：吸盘矩形越界不再作为真实装箱风险。"""
        risks = []
        tol = 1e-6
        if row["x"] < -tol or row["y"] < -tol or row["z"] < -tol or row["x"] + row["lx"] > L + tol or row["y"] + row["ly"] > W + tol or row["z"] + row["lz"] > H + tol:
            risks.append("箱体越界")
        sr = safe_float(row.get("support_ratio"), 0.0)
        if sr < 0.70:
            risks.append("严重支撑不足")
        elif sr < 0.90:
            risks.append("支撑偏低")
        pu = row.get("pressure_utilization", float("nan"))
        try:
            if not math.isnan(float(pu)) and safe_float(pu) > 1.0:
                risks.append("承压超限")
        except Exception:
            pass
        if row.get("suction_box_corner") in {"x_min_y_min", "x_min_y_max", "x_max_y_min", "x_max_y_max"} and row.get("suction_cup_corner") in {"x_min_y_min", "x_min_y_max", "x_max_y_min", "x_max_y_max"}:
            if row.get("suction_box_corner") != row.get("suction_cup_corner"):
                risks.append("吸附角点不一致")
        return "；".join(risks) if risks else "正常"

    def box_risk_level(self, text: str) -> int:
        text = normalize_risk_text(text)
        if text == "正常":
            return 0
        if "严重" in text or "超限" in text or "箱体越界" in text:
            return 2
        return 1

    def _current_selected_box_key(self):
        try:
            if self.df_eval is None or self.selected_row_index is None or self.selected_row_index not in self.df_eval.index:
                return None
            r = self.df_eval.loc[self.selected_row_index]
            return {
                "seq": int(r.get("seq")),
                "box_id": safe_str(r.get("box_id"), ""),
                "box_type": safe_str(r.get("box_type"), ""),
                "x": safe_float(r.get("x"), float("nan")),
                "y": safe_float(r.get("y"), float("nan")),
                "z": safe_float(r.get("z"), float("nan")),
                "lx": safe_float(r.get("lx"), float("nan")),
                "ly": safe_float(r.get("ly"), float("nan")),
                "lz": safe_float(r.get("lz"), float("nan")),
            }
        except Exception:
            return None

    def _select_box_row_by_df_index(self, df_index, switch_to_table: bool = False) -> bool:
        if not hasattr(self, "box_table"):
            return False
        try:
            target = int(df_index)
        except Exception:
            return False
        for row in range(self.box_table.rowCount()):
            item = self.box_table.item(row, 0)
            if item is None:
                continue
            try:
                if int(item.data(QtCore.Qt.UserRole)) == target:
                    self.box_table.setCurrentCell(row, 0)
                    self.box_table.selectRow(row)
                    if switch_to_table and hasattr(self, "workspace_tabs"):
                        self.workspace_tabs.setCurrentIndex(1)
                    return True
            except Exception:
                continue
        if self.df_eval is not None and target in self.df_eval.index:
            self.selected_row_index = target
            self.update_selected_box_detail()
            self.refresh_3d_scene()
            return True
        return False

    def _select_box_by_identifier(self, box_id: str, switch_to_table: bool = False) -> bool:
        box_id = safe_str(box_id, "").strip()
        if not box_id or box_id in {"--", "-"} or self.df_eval is None:
            return False
        for idx, row in self.df_eval.iterrows():
            candidates = {
                safe_str(row.get("box_id"), ""),
                safe_str(row.get("seq"), ""),
            }
            if box_id in candidates:
                return self._select_box_row_by_df_index(idx, switch_to_table=switch_to_table)
        return False

    def on_warning_item_clicked(self, item) -> None:
        if item is None:
            return
        idx = item.data(QtCore.Qt.UserRole)
        if idx is None:
            return
        self._select_box_row_by_df_index(idx, switch_to_table=False)
        if hasattr(self, "workspace_tabs"):
            self.workspace_tabs.setCurrentIndex(0)

    def fill_warning_list(self) -> None:
        # 重写右侧风险列表：过滤“吸盘矩形越界”，并给每个风险箱绑定 df 行号，点击即可在 3D 中高亮。
        self.warning_list.clear()
        if self.current_pallet is None or self.df_eval is None or self.score_result is None:
            return
        p = self.current_pallet
        if safe_str(p.get("mpm_status"), "UNKNOWN") != "SUCCESS":
            self.warning_list.addItem(f"托盘指数预警：mpm_status={safe_str(p.get('mpm_status'))}, gap={safe_float(p.get('mpm_gap'), 0):g}")
        if self.score_result.get("height_utilization", 0) > 1.0:
            self.warning_list.addItem("高度预警：箱垛最高点超过托盘限高。")
        total_score = float(self.score_result.get("total_score", 0))
        if total_score < 70:
            self.warning_list.addItem(f"稳定性预警：综合评分 {total_score:.1f}，建议复核支撑和重心。")

        risk_df = self.df_eval[self.df_eval["risk_level"] > 0]
        if len(risk_df) == 0:
            self.warning_list.addItem("当前托盘未发现明显箱体级风险。")
            self._update_suggestions()
            return
        for idx, row in risk_df.sort_values(["risk_level", "support_ratio"], ascending=[False, True]).head(40).iterrows():
            txt = normalize_risk_text(row.get("risk_text", "正常"))
            if txt == "正常":
                continue
            qitem = QtWidgets.QListWidgetItem(f"第{int(row['seq'])}箱 / {row['box_id']} / {row['box_type']}：{txt}")
            qitem.setData(QtCore.Qt.UserRole, int(idx))
            self.warning_list.addItem(qitem)
        if self.warning_list.count() == 0:
            self.warning_list.addItem("当前托盘未发现明显箱体级风险。")
        self._update_suggestions()

    def show_final_result(self) -> None:
        """Display all boxes immediately instead of playing the animation.

        兼容原始 stability_business_dashboard_json.py：
        有些用户只替换 realtime_dashboard_v2.py，原始基类里没有这个方法，
        所以这里在子类中补齐，避免启动时 AttributeError。
        """
        if getattr(self, "df_eval", None) is None:
            return
        try:
            if not getattr(self, "animation_order", []):
                self.prepare_animation_order()
            self.animation_idx = len(getattr(self, "animation_order", []))
            if hasattr(self, "anim_timer"):
                self.anim_timer.stop()
            self.refresh_3d_scene()
            if hasattr(self, "workspace_tabs"):
                self.workspace_tabs.setCurrentIndex(0)
            if hasattr(self, "_write_log"):
                self._write_log("[UI] 已直接显示最终装箱结果。")
        except Exception as exc:
            if hasattr(self, "show_error"):
                self.show_error(f"显示最终结果失败：{exc}")
            else:
                raise

    def on_visual_pallet_changed(self, idx: int) -> None:
        if idx < 0 or not hasattr(self, "cmb_pallet"):
            return
        if idx >= self.cmb_pallet.count():
            return
        if self.cmb_pallet.currentIndex() != idx:
            self.cmb_pallet.setCurrentIndex(idx)
        elif 0 <= idx < len(getattr(self, "filtered_pallets", [])):
            self.load_pallet(self.filtered_pallets[idx])

    def on_failed_table_selection_changed(self) -> None:
        """单击失败列表行时，立即切换/高亮对应托盘；如果有箱号则尝试高亮箱体。"""
        try:
            if getattr(self, "_handling_failed_selection", False):
                return
            if not hasattr(self, "failed_table"):
                return
            row = self.failed_table.currentRow()
            if row < 0:
                return
            pallet_item = self.failed_table.item(row, 1)
            box_item = self.failed_table.item(row, 4)
            if pallet_item is None:
                return
            target = pallet_item.data(QtCore.Qt.UserRole) or pallet_item.text()
            target = safe_str(target, "").strip()
            if not target or target in {"--", "-"}:
                return
            box_id = safe_str(box_item.text() if box_item else "", "").strip()
            self._focus_failed_target(target, box_id)
        except Exception as exc:
            if hasattr(self, "_write_log"):
                self._write_log(f"[UI] 失败列表选择联动失败：{exc}")

    def _focus_failed_target(self, target: str, box_id: str = "") -> bool:
        target = safe_str(target, "").strip()
        if not target or target in {"--", "-"}:
            return False
        self._handling_failed_selection = True
        try:
            # 优先从全部托盘中找，避免当前筛选条件把失败托盘隐藏后无法跳转。
            target_pallet = None
            for p in getattr(self, "pallets", []) or []:
                if safe_str(p.get("pallet_id"), "") == target:
                    target_pallet = p
                    break
            if target_pallet is None:
                for p in getattr(self, "filtered_pallets", []) or []:
                    if safe_str(p.get("pallet_id"), "") == target:
                        target_pallet = p
                        break
            if target_pallet is None:
                return False

            # 如果目标不在当前筛选结果中，重置筛选到“全部”，保证中间 6 托盘区也能看到它。
            if all(safe_str(p.get("pallet_id"), "") != target for p in getattr(self, "filtered_pallets", []) or []):
                try:
                    if hasattr(self, "cmb_status"):
                        self.cmb_status.blockSignals(True)
                        self.cmb_status.setCurrentIndex(0)
                        self.cmb_status.blockSignals(False)
                    if hasattr(self, "cmb_order"):
                        self.cmb_order.blockSignals(True)
                        self.cmb_order.setCurrentIndex(0)
                        self.cmb_order.blockSignals(False)
                    if hasattr(self, "search_edit"):
                        self.search_edit.blockSignals(True)
                        self.search_edit.clear()
                        self.search_edit.blockSignals(False)
                    self.refresh_pallet_filter()
                except Exception:
                    pass

            # 切换当前托盘。
            filtered_index = 0
            for i, p in enumerate(getattr(self, "filtered_pallets", []) or []):
                if safe_str(p.get("pallet_id"), "") == target:
                    filtered_index = i
                    break
            if hasattr(self, "cmb_pallet") and filtered_index < self.cmb_pallet.count():
                self.cmb_pallet.setCurrentIndex(filtered_index)
            else:
                self.load_pallet(target_pallet)

            # 翻到目标托盘所在页，并回到 3D 视图。
            try:
                self.overview_page = filtered_index // 6
            except Exception:
                pass
            if hasattr(self, "workspace_tabs"):
                self.workspace_tabs.setCurrentIndex(0)
            self._populate_overview_cards()

            # 若失败列表行带有箱号，则尝试进一步高亮该箱体。
            if box_id and box_id not in {"--", "-"}:
                self._select_box_by_identifier(box_id, switch_to_table=False)
            else:
                self.selected_row_index = None
                self.refresh_3d_scene()
            return True
        finally:
            self._handling_failed_selection = False

    def on_failed_table_double_clicked(self, row: int, column: int) -> None:
        """Jump from the failed-list row to the corresponding pallet.

        失败列表是新增页面，原始基类没有这个槽函数。这里补齐后，双击失败列表
        中的任意行会切换到对应托盘，并跳回 3D 装箱视图。
        """
        try:
            if not hasattr(self, "failed_table"):
                return
            item = self.failed_table.item(row, 1)  # 第 2 列：托盘
            if item is None:
                return
            target = item.data(QtCore.Qt.UserRole) or item.text()
            target = str(target).strip()
            if not target or target in {"--", "-"}:
                return

            # 优先从左侧托盘下拉框里找。下拉项可能是纯 pallet_id，
            # 也可能是“pallet_id | 状态 | 数量”这类展示文本，所以做包含匹配。
            if hasattr(self, "cmb_pallet"):
                for i in range(self.cmb_pallet.count()):
                    text = self.cmb_pallet.itemText(i)
                    data = self.cmb_pallet.itemData(i)
                    if target == str(data).strip() or target == text.strip() or target in text:
                        self.cmb_pallet.setCurrentIndex(i)
                        if hasattr(self, "workspace_tabs"):
                            self.workspace_tabs.setCurrentIndex(0)
                        return

            # 兜底：直接在 filtered_pallets / pallets 里按 pallet_id 查找。
            for source_name in ("filtered_pallets", "pallets"):
                for idx, pallet in enumerate(getattr(self, source_name, []) or []):
                    pid = str(pallet.get("pallet_id", pallet.get("id", ""))).strip()
                    if pid == target:
                        if hasattr(self, "cmb_pallet") and idx < self.cmb_pallet.count():
                            self.cmb_pallet.setCurrentIndex(idx)
                        else:
                            self.load_pallet(pallet)
                        if hasattr(self, "workspace_tabs"):
                            self.workspace_tabs.setCurrentIndex(0)
                        return

            QtWidgets.QMessageBox.information(self, "提示", f"没有找到对应托盘：{target}")
        except Exception as exc:
            if hasattr(self, "show_error"):
                self.show_error(f"跳转失败列表对应托盘失败：{exc}")
            else:
                raise

    def on_pallet_changed(self, idx: int):
        super().on_pallet_changed(idx)
        if hasattr(self, "cmb_visual_pallet") and hasattr(self, "cmb_pallet"):
            self.cmb_visual_pallet.blockSignals(True)
            self.cmb_visual_pallet.setCurrentIndex(self.cmb_pallet.currentIndex())
            self.cmb_visual_pallet.blockSignals(False)

    def refresh_pallet_filter(self):
        super().refresh_pallet_filter()
        self._sync_visual_pallet_combo()
        if hasattr(self, "zoom_box") and self.zoom_box.isVisible():
            self._exit_pallet_zoom()
        self.overview_page = 0
        self._populate_overview_cards()

    def _toggle_param_panel(self) -> None:
        checked = self.btn_toggle_params.isChecked()
        self.param_box.setVisible(checked)
        self.btn_toggle_params.setText("收起高级参数" if checked else "展开高级参数")
        self.btn_toggle_params.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def load_pallet(self, pallet: dict):
        super().load_pallet(pallet)
        self._sync_visual_pallet_combo()
        self._populate_overview_cards()

    def refresh_3d_scene(self, *args):
        # 主界面不再绘制单个大 3D 图；六个托盘卡片各自维护独立 3D 视图。
        self._populate_overview_cards()
        if hasattr(self, "zoom_canvas") and getattr(self, "zoom_pallet", None):
            try:
                self.zoom_canvas.set_selected_box_key(self._current_selected_box_key(), render=True)
            except Exception:
                pass
        if hasattr(self, "anim_label"):
            try:
                self.anim_label.setText(f"进度：{len(getattr(self, 'animation_order', []))} / {len(getattr(self, 'animation_order', []))}")
            except Exception:
                pass

    def _page_count(self) -> int:
        total = len(getattr(self, "filtered_pallets", []) or [])
        return max(1, (total + 5) // 6)

    def _populate_overview_cards(self) -> None:
        if not hasattr(self, "overview_cards"):
            return
        pallets = list(getattr(self, "filtered_pallets", []) or [])
        pages = self._page_count()
        self.overview_page = max(0, min(self.overview_page, pages - 1))
        start = self.overview_page * 6
        page_items = pallets[start:start + 6]
        current_id = safe_str((self.current_pallet or {}).get("pallet_id"), "") if getattr(self, "current_pallet", None) else ""
        selected_key = self._current_selected_box_key()
        for i, card in enumerate(self.overview_cards):
            pallet = page_items[i] if i < len(page_items) else None
            is_current = bool(pallet and safe_str(pallet.get("pallet_id"), "") == current_id)
            card.set_data(pallet, is_current, selected_key if is_current else None)
        page_text = f"第 {self.overview_page + 1} / {pages} 页"
        for name in ["lbl_overview_page", "lbl_left_page"]:
            if hasattr(self, name):
                getattr(self, name).setText(page_text)
        for name in ["btn_prev_page", "btn_left_prev_page"]:
            if hasattr(self, name):
                getattr(self, name).setEnabled(self.overview_page > 0)
        for name in ["btn_next_page", "btn_left_next_page"]:
            if hasattr(self, name):
                getattr(self, name).setEnabled(self.overview_page < pages - 1)

    def _prev_overview_page(self) -> None:
        if self.overview_page > 0:
            self.overview_page -= 1
            self._populate_overview_cards()

    def _next_overview_page(self) -> None:
        if self.overview_page < self._page_count() - 1:
            self.overview_page += 1
            self._populate_overview_cards()

    def _on_overview_card_clicked(self, local_index: int) -> None:
        pallets = list(getattr(self, "filtered_pallets", []) or [])
        global_index = self.overview_page * 6 + int(local_index)
        if global_index < 0 or global_index >= len(pallets):
            return
        self.load_pallet(pallets[global_index])
        if hasattr(self, "cmb_pallet"):
            self.cmb_pallet.blockSignals(True)
            self.cmb_pallet.setCurrentIndex(global_index)
            self.cmb_pallet.blockSignals(False)
        self._sync_visual_pallet_combo()
        self._populate_overview_cards()

    def _enter_pallet_zoom(self, pallet) -> None:
        if not pallet:
            return
        self._on_overview_card_clicked(self._local_index_for_pallet(pallet))
        self.zoom_pallet = pallet
        self.zoom_anim_index = len(pallet.get("packed_items", []) or [])
        items = pallet.get("packed_items", []) or []
        status = safe_str(pallet.get("mpm_status"), "UNKNOWN")
        fill_rate = safe_float(pallet.get("fill_rate"), float("nan"))
        fill_txt = "--" if math.isnan(fill_rate) else f"{fill_rate * 100:.1f}%"
        mpm_total = safe_float(pallet.get("mpm_total"), float("nan"))
        mpm_target = safe_float(pallet.get("mpm_target"), float("nan"))
        mpm_gap = safe_float(pallet.get("mpm_gap"), 0.0)
        index_txt = f"{mpm_total:g}/{mpm_target:g}" if (not math.isnan(mpm_total) and not math.isnan(mpm_target) and mpm_target > 0) else status

        self.zoom_title.setText(safe_str(pallet.get("pallet_id"), "--"))
        self.zoom_sub.setText(f"状态：{status} ｜ 箱数：{len(items)} ｜ 填充率：{fill_txt} ｜ 指数：{index_txt} ｜ 缺口：{mpm_gap:g}")
        self.zoom_canvas.set_pallet(pallet)
        self._apply_zoom_options()

        if hasattr(self, "overview_box"):
            self.overview_box.setVisible(False)
        if hasattr(self, "zoom_box"):
            self.zoom_box.setVisible(True)
        if hasattr(self, "workspace_tabs"):
            self.workspace_tabs.setCurrentIndex(0)

    def _local_index_for_pallet(self, pallet) -> int:
        try:
            pallets = list(getattr(self, "filtered_pallets", []) or [])
            pid = safe_str(pallet.get("pallet_id"), "")
            for global_index, p in enumerate(pallets):
                if safe_str(p.get("pallet_id"), "") == pid:
                    self.overview_page = global_index // 6
                    return global_index % 6
        except Exception:
            pass
        return 0

    def _exit_pallet_zoom(self) -> None:
        if hasattr(self, "zoom_timer"):
            self.zoom_timer.stop()
        if hasattr(self, "zoom_box"):
            self.zoom_box.setVisible(False)
        if hasattr(self, "overview_box"):
            self.overview_box.setVisible(True)
        self._populate_overview_cards()

    def _apply_zoom_options(self, reset_camera: bool = False) -> None:
        if not getattr(self, "zoom_pallet", None):
            return
        visible = None
        items = self.zoom_pallet.get("packed_items", []) or []
        if self.zoom_anim_index < len(items):
            visible = max(0, int(self.zoom_anim_index))
        self.zoom_canvas.set_selected_box_key(self._current_selected_box_key(), render=False)
        self.zoom_canvas.set_options(
            show_suction=self.zoom_chk_suction.isChecked(),
            only_risk=self.zoom_chk_risk.isChecked(),
            color_mode=self.zoom_cmb_color.currentText(),
            visible_count=visible,
            reset_camera=reset_camera,
        )

    def _zoom_show_final(self) -> None:
        if not getattr(self, "zoom_pallet", None):
            return
        self.zoom_timer.stop()
        self.zoom_anim_index = len(self.zoom_pallet.get("packed_items", []) or [])
        self._apply_zoom_options()

    def _zoom_play(self) -> None:
        if not getattr(self, "zoom_pallet", None):
            return
        self.zoom_anim_index = 0
        self._apply_zoom_options()
        self.zoom_timer.start(420)

    def _zoom_pause(self) -> None:
        self.zoom_timer.stop()

    def _zoom_reset(self) -> None:
        if not getattr(self, "zoom_pallet", None):
            return
        self.zoom_timer.stop()
        self.zoom_anim_index = 0
        self._apply_zoom_options(reset_camera=True)

    def _zoom_anim_step(self) -> None:
        if not getattr(self, "zoom_pallet", None):
            self.zoom_timer.stop()
            return
        items = self.zoom_pallet.get("packed_items", []) or []
        if self.zoom_anim_index >= len(items):
            self.zoom_timer.stop()
            return
        self.zoom_anim_index += max(1, len(items) // 80)
        self._apply_zoom_options()

    # ------------------------------------------------------------- backend
    def _write_log(self, text: str) -> None:
        if hasattr(self, "log_box"):
            self.log_box.appendPlainText(str(text))
            cursor = self.log_box.textCursor()
            cursor.movePosition(cursor.End)
            self.log_box.setTextCursor(cursor)

    def _set_status(self, state: str, msg: Optional[str] = None) -> None:
        text_map = {
            "idle": "空闲",
            "running": "运行中",
            "done": "已完成",
            "error": "异常",
            "stopped": "已停止",
        }
        if hasattr(self, "status_pill"):
            self.status_pill.set_state(state, msg or text_map.get(state, state))
        if hasattr(self, "run_progress"):
            if state == "idle":
                self.run_progress.setValue(0)
                self.run_progress.setFormat("等待开始")
            elif state == "running":
                self.run_progress.setRange(0, 0)
                self.run_progress.setFormat("后端计算中")
            else:
                self.run_progress.setRange(0, 100)
                self.run_progress.setValue(100 if state == "done" else 0)
                self.run_progress.setFormat(text_map.get(state, state))

    def choose_project_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择装箱算法项目目录", str(self.project_dir))
        if not path:
            return
        self.project_dir = Path(path).resolve()
        self.config_path = self.project_dir / DEFAULT_CONFIG_REL
        self._write_log(f"[UI] 已切换算法目录：{self.project_dir}")
        self._write_log(f"[UI] 默认配置：{self.config_path}")

    def choose_config_file(self) -> None:
        start = self.config_path.parent if self.config_path else self.project_dir
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择装箱算法配置 YAML", str(start), "YAML Files (*.yaml *.yml);;All Files (*.*)"
        )
        if not path:
            return
        self.config_path = Path(path).resolve()
        self._write_log(f"[UI] 已选择配置：{self.config_path}")

    def start_backend_packing(self) -> None:
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "提示", "后端装箱正在运行。")
            return
        self.worker = PackingWorker(self.project_dir, self.config_path, self)
        self.worker.log.connect(self._write_log)
        self.worker.started_cmd.connect(lambda cmd: self._write_log(f"[CMD] {cmd}"))
        self.worker.failed.connect(self.on_backend_failed)
        self.worker.finished_json.connect(self.on_backend_finished_json)
        self.worker.finished.connect(self.on_worker_finished)

        self.btn_start_backend.setEnabled(False)
        self.btn_stop_backend.setEnabled(True)
        self.btn_load.setEnabled(False)
        self.step_run.set_state("active", "后端装箱算法正在运行")
        self._set_status("running")
        self._write_log("[UI] 开始后端装箱计算，界面保持可操作。")
        self.worker.start()

    def stop_backend_packing(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._write_log("[UI] 正在停止后端装箱进程...")
            self.step_run.set_state("error", "已请求停止后端进程")
            self._set_status("stopped")

    def on_worker_finished(self) -> None:
        self.btn_start_backend.setEnabled(True)
        self.btn_stop_backend.setEnabled(False)
        self.btn_load.setEnabled(True)

    def on_backend_failed(self, msg: str) -> None:
        self._write_log(f"[错误] {msg}")
        if hasattr(self, "btn_toggle_log"):
            self.btn_toggle_log.setChecked(True)
            self.toggle_bottom_log()
        self.step_run.set_state("error", "后端运行失败，请查看底部日志")
        self._set_status("error")
        QtWidgets.QMessageBox.critical(self, "后端装箱失败", msg)

    def on_backend_finished_json(self, json_path: str) -> None:
        path = Path(json_path)
        self._write_log(f"[UI] 后端装箱完成，自动加载结果：{path}")
        try:
            self.load_json_file(path)
            self.animation_idx = 0
            self.show_final_result()
            self.step_run.set_state("done", "后端完成，已显示最终装箱结果")
            self.step_result.set_state("done", f"结果文件：{path.name}")
            self._set_status("done")
        except Exception as exc:
            self.on_backend_failed(f"加载算法输出 JSON 失败：{exc}")

    def open_latest_result(self) -> None:
        latest = find_latest_json(self.project_dir)
        if latest is None:
            QtWidgets.QMessageBox.warning(self, "没有找到结果", "没有找到 packing_plan_*.json 或其他 JSON 输出。")
            return
        self._write_log(f"[UI] 手动加载最新结果：{latest}")
        self.load_json_file(latest)
        self.show_final_result()
        self.step_result.set_state("done", f"结果文件：{latest.name}")
        self._set_status("done")

    # ------------------------------------------------------------- glue/reuse
    def _make_hidden_base_buttons(self) -> None:
        """Base methods expect these attributes. They are used by shortcuts/actions only."""
        self.btn_load = QtWidgets.QPushButton("打开装箱结果")
        self.btn_load.clicked.connect(self.load_json_dialog)
        self.btn_recalc = QtWidgets.QPushButton("重新评估稳定性")
        self.btn_recalc.clicked.connect(self.recalculate_current)
        self.btn_export = QtWidgets.QPushButton("导出托盘分析")
        self.btn_export.clicked.connect(self.export_eval_table)

    def fill_pallet_cards(self) -> None:
        super().fill_pallet_cards()
        if getattr(self, "current_pallet", None) is not None:
            if hasattr(self, "card_fill"):
                fill_rate = safe_float(self.current_pallet.get("fill_rate"), float("nan"))
                has_fill = not math.isnan(fill_rate)
                fill_txt = "--" if not has_fill else f"{fill_rate * 100:.1f}%"
                state = "normal" if not has_fill else ("good" if fill_rate >= 0.85 else ("warn" if fill_rate >= 0.70 else "bad"))
                self.card_fill.set_data(fill_txt, "托盘空间利用率", state)
            if hasattr(self, "card_mpm"):
                mpm_status = safe_str(self.current_pallet.get("mpm_status"), "UNKNOWN")
                mpm_gap = safe_float(self.current_pallet.get("mpm_gap"), 0.0)
                mpm_total = safe_float(self.current_pallet.get("mpm_total"), float("nan"))
                mpm_target = safe_float(self.current_pallet.get("mpm_target"), float("nan"))
                if not math.isnan(mpm_total) and not math.isnan(mpm_target) and mpm_target > 0:
                    index_value = f"{mpm_total:g}/{mpm_target:g}"
                else:
                    index_value = mpm_status
                self.card_mpm.set_data(index_value, f"状态 {mpm_status}，缺口 {mpm_gap:g}", "good" if mpm_status == "SUCCESS" else "bad")
        self._update_v2_summary()

    def populate_after_load(self) -> None:
        super().populate_after_load()
        self.step_result.set_state("done", "可以选择托盘查看详情")
        if hasattr(self, "card_avg_fill"):
            vals = [safe_float(p.get("fill_rate"), float("nan")) for p in getattr(self, "pallets", [])]
            vals = [v for v in vals if not math.isnan(v)]
            avg = sum(vals) / len(vals) if vals else float("nan")
            self.card_avg_fill.set_data("--" if math.isnan(avg) else f"{avg * 100:.1f}%", "全局平均托盘空间利用率", "good" if (not math.isnan(avg) and avg >= 0.85) else "normal")
        self._populate_overview_cards()
        self._set_status("done")

    def clear_current_views(self) -> None:
        super().clear_current_views()
        if hasattr(self, "lbl_pallet_id"):
            self.lbl_pallet_id.setText("--")
            self.lbl_pallet_sub.setText("请先开始装箱或打开结果")
        if hasattr(self, "suggestion_label"):
            self.suggestion_label.setText("等待装箱结果。完成后这里会用人话解释主要风险。")
        for kpi in ["kpi_score_big", "kpi_level_big", "kpi_support_big", "kpi_risk_big"]:
            if hasattr(self, kpi):
                getattr(self, kpi).set_data("--", "等待结果")
        for card_name in ["card_score", "card_level", "card_boxes", "card_mass", "card_fill", "card_mpm", "card_height", "card_support", "card_cg", "card_avg_fill"]:
            if hasattr(self, card_name):
                getattr(self, card_name).set_data("--", "--")
        if hasattr(self, "overview_cards"):
            for card in self.overview_cards:
                card.set_data(None, False)

    def _update_v2_summary(self) -> None:
        if self.current_pallet is None or self.df_eval is None or self.score_result is None:
            return
        p = self.current_pallet
        df = self.df_eval
        s = self.score_result
        pallet_id = safe_str(p.get("pallet_id"), "--")
        status = safe_str(p.get("mpm_status"), "UNKNOWN")
        total_score = float(s.get("total_score", 0.0))
        level = score_level(total_score)
        risk_count = int((df["risk_level"] > 0).sum()) if "risk_level" in df.columns else 0
        support_avg = float(df["support_ratio"].mean()) if len(df) else 0.0

        self.lbl_pallet_id.setText(pallet_id)
        fill_rate = safe_float(p.get("fill_rate"), float("nan"))
        fill_txt = "--" if math.isnan(fill_rate) else f"{fill_rate * 100:.1f}%"
        self.lbl_pallet_sub.setText(f"指数：{status} | 箱数：{len(df)} | 填充率：{fill_txt} | 风险箱：{risk_count}")

        level_state = "good" if total_score >= 70 else ("warn" if total_score >= 60 else "bad")
        support_state = "good" if support_avg >= 0.95 else ("warn" if support_avg >= 0.90 else "bad")
        risk_state = "good" if risk_count == 0 else ("warn" if risk_count <= 3 else "bad")
        self.kpi_score_big.set_data(f"{total_score:.1f}", "综合稳定性", level_state)
        self.kpi_level_big.set_data(level, "A最好，D风险", level_state)
        self.kpi_support_big.set_data(f"{support_avg * 100:.1f}%", "按接触面积重算", support_state)
        self.kpi_risk_big.set_data(str(risk_count), "风险箱数量", risk_state)

    def _update_suggestions(self) -> None:
        if self.df_eval is None or self.score_result is None:
            return
        df = self.df_eval
        score = float(self.score_result.get("total_score", 0.0))
        risk_count = int((df["risk_level"] > 0).sum()) if "risk_level" in df.columns else 0
        serious_count = int((df["risk_level"] > 1).sum()) if "risk_level" in df.columns else 0
        support_avg = float(df["support_ratio"].mean()) if len(df) else 0.0
        height_util = float(self.score_result.get("height_utilization", 0.0))

        if score >= 85 and risk_count == 0:
            text = "当前托盘稳定性优秀，没有发现明显箱体级风险。可以优先作为装箱结果展示。"
        elif score >= 70 and serious_count == 0:
            text = "当前托盘整体可用。建议重点看黄色风险箱，并结合指数与填充率确认是否满足现场要求。"
        else:
            text = "当前托盘存在稳定性风险。建议优先复核红色风险箱、支撑不足位置和指数异常原因。"

        details = []
        if support_avg < 0.90:
            details.append("平均支撑率偏低")
        if height_util > 1.0:
            details.append("高度超过托盘限高")
        if serious_count > 0:
            details.append(f"严重风险箱 {serious_count} 个")
        if details:
            text += " 主要问题：" + "、".join(details) + "。"
        self.suggestion_label.setText(text)

    def add_cg_marker(self):
        # 当前界面不再在 3D 视图里画重心点，避免干扰装箱结果观察。
        return

    def add_cg_projection_marker(self, L: float, W: float):
        # 当前界面不再在 3D 视图里画重心投影；重心偏移保留在右侧指标卡中。
        return

    def closeEvent(self, event) -> None:
        try:
            self.stop_backend_packing()
        except Exception:
            pass
        super().closeEvent(event)


# Patch BaseDashboard.__init__ call: _build_ui uses hidden buttons expected by methods.
_original_build_ui = IndustrialPackingWorkbench._build_ui

def _patched_build_ui(self: IndustrialPackingWorkbench) -> None:
    self._make_hidden_base_buttons()
    _original_build_ui(self)

IndustrialPackingWorkbench._build_ui = _patched_build_ui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Industrial realtime packing dashboard v2")
    parser.add_argument("--project", default=str(_PROJECT_DIR_DEFAULT), help="zhuangxiang_code project directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Industrial Packing Workbench V2")
    win = IndustrialPackingWorkbench(Path(args.project))
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
