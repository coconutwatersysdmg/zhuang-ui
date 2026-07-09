# -*- coding: utf-8 -*-
r"""
Industrial Packing Workbench V3 Clean

核心目标：
1. 直接选择 Excel，不需要手动修改 packing_config.yaml；
2. 自动复制输入 Excel 到 data/ui_inputs，避免中文路径/空格路径带来的问题；
3. 自动生成 runtime/packing-realtime/temp 下的临时 YAML；
4. 后端运行时强制追加 --out，把 JSON 输出到 runtime/packing-realtime/exports；
5. 后端完成后直接加载这个 JSON 到界面，不再让用户手动找 packing_plan_*.json。

推荐运行：
    python apps/realtime_dashboard/realtime_dashboard_v3_clean.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# -----------------------------------------------------------------------------
# Import v2 safely. v2 already contains the Qt plugin path fix and UI theme.
# -----------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_THIS_DIR = _THIS_FILE.parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Please install PyYAML in packing-realtime venv.") from exc

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("pandas/openpyxl are required for Excel sheet detection.") from exc

try:
    from PyQt5 import QtCore, QtWidgets
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyQt5 is required. Please run with the packing-realtime venv.") from exc

try:
    from realtime_dashboard_v2 import (
        IndustrialPackingWorkbench,
        StatusPill,
        DEFAULT_CONFIG_REL,
        DEFAULT_RUN_SCRIPT_REL,
        RUNTIME_NAME,
        _PROJECT_DIR_DEFAULT,
        ensure_runtime_dirs,
        runtime_dir_from_project,
        log_dir_from_project,
        find_latest_json,
    )
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "Cannot import realtime_dashboard_v2.py. Keep this file in apps/realtime_dashboard."
    ) from exc


REQUIRED_INCREMENTAL_SHEETS = {"最终挑选结果", "新增箱", "包装物料主数据(BMS)"}
BMS_SHEET = "包装物料主数据(BMS)"


def _safe_ascii_stem(name: str, default: str = "input") -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._-")
    return cleaned[:80] or default


def _project_data_dir(project_dir: Path) -> Path:
    return Path(project_dir).resolve() / "data"


def _ui_inputs_dir(project_dir: Path) -> Path:
    return _project_data_dir(project_dir) / "ui_inputs"


def _runtime_temp_dir(project_dir: Path) -> Path:
    return runtime_dir_from_project(project_dir) / "temp"


def _runtime_exports_dir(project_dir: Path) -> Path:
    return runtime_dir_from_project(project_dir) / "exports"


def _relative_to_data(project_dir: Path, path: Path) -> str:
    data_dir = _project_data_dir(project_dir).resolve()
    return Path(path).resolve().relative_to(data_dir).as_posix()


def _copy_excel_to_project_data(project_dir: Path, excel_path: Path) -> Path:
    excel_path = Path(excel_path).resolve()
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file does not exist: {excel_path}")
    out_dir = _ui_inputs_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = excel_path.suffix.lower() if excel_path.suffix else ".xlsx"
    safe_stem = _safe_ascii_stem(excel_path.name)
    dst = out_dir / f"{stamp}_{safe_stem}{suffix}"
    shutil.copy2(excel_path, dst)
    return dst


def _read_excel_mode(excel_path: Path) -> Tuple[str, list[str], list[str]]:
    """Return (run_mode, sheet_names, warnings)."""
    excel_path = Path(excel_path)
    excel = pd.ExcelFile(excel_path)
    sheets = list(excel.sheet_names)
    sheet_set = set(sheets)
    warnings = []

    if "新增箱" in sheet_set:
        mode = "incremental"
        missing = sorted(REQUIRED_INCREMENTAL_SHEETS - sheet_set)
        if missing:
            warnings.append("增量模式缺少工作表：" + "、".join(missing))
    else:
        mode = "normal"
        if BMS_SHEET not in sheet_set:
            warnings.append("普通模式建议包含工作表：包装物料主数据(BMS)")
        if len([s for s in sheets if s not in {BMS_SHEET, "说明"}]) == 0:
            warnings.append("没有发现可作为订单数据的工作表。")
    return mode, sheets, warnings


def _load_yaml(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Base config does not exist: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML config: {path}")
    return data


def _write_ui_config(project_dir: Path, base_config_path: Path, excel_copy_path: Path, run_mode: str) -> Path:
    config = _load_yaml(base_config_path)
    rel_source = _relative_to_data(project_dir, excel_copy_path)

    config["run_mode"] = run_mode
    config.setdefault("excel_data", {})
    config.setdefault("incremental", {})

    # 同时写两个字段，保证用户切换 normal / incremental 时不用再次改配置。
    config["excel_data"]["source_file"] = rel_source
    config["incremental"]["source_file"] = rel_source

    temp_dir = _runtime_temp_dir(project_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg_path = temp_dir / f"ui_config_{run_mode}_{stamp}.yaml"
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    return cfg_path


def _make_out_path(project_dir: Path, prefix: str = "ui_packing_plan") -> Path:
    out_dir = _runtime_exports_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return out_dir / f"{prefix}_{stamp}.json"


def _is_valid_packing_json(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict) and isinstance(data.get("pallets"), list)
    except Exception:
        return False


class UiPackingWorker(QtCore.QThread):
    """Run backend with an explicit --out JSON path, then emit that exact file."""

    log = QtCore.pyqtSignal(str)
    started_cmd = QtCore.pyqtSignal(str)
    finished_json = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, project_dir: Path, config_path: Path, out_path: Path, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir).resolve()
        self.config_path = Path(config_path).resolve()
        self.out_path = Path(out_path).resolve()
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

    def _emit_log(self, text: str) -> None:
        self.log.emit(text)
        self._write_backend_log(text)

    def run(self) -> None:
        try:
            run_script = self.project_dir / DEFAULT_RUN_SCRIPT_REL
            if not run_script.exists():
                self.failed.emit(f"找不到装箱算法入口：{run_script}")
                return
            if not self.config_path.exists():
                self.failed.emit(f"找不到配置文件：{self.config_path}")
                return

            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            if self.out_path.exists():
                try:
                    self.out_path.unlink()
                except Exception:
                    pass

            cmd = [
                sys.executable,
                str(run_script),
                "--config",
                str(self.config_path),
                "--out",
                str(self.out_path),
            ]
            cmd_text = " ".join(f'"{x}"' if " " in x else x for x in cmd)
            self.started_cmd.emit(cmd_text)
            self._emit_log(f"[LOG] 后端日志文件：{self.log_file}")
            self._emit_log(f"[LOG] 本次结果将输出到：{self.out_path}")
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
                    self._emit_log("[UI] 已请求停止后端装箱。")
                    return
                msg = line.rstrip()
                if msg:
                    self._emit_log(msg)

            code = self.process.wait()
            if self._stop_requested:
                self._emit_log("[UI] 后端装箱已停止。")
                return
            if code != 0:
                self.failed.emit(f"装箱算法运行失败，退出码：{code}")
                return

            time.sleep(0.3)
            if self.out_path.exists() and _is_valid_packing_json(self.out_path):
                self.finished_json.emit(str(self.out_path))
                return

            # Fallback: useful if the backend changed its output behavior.
            latest = find_latest_json(self.project_dir)
            if latest and _is_valid_packing_json(latest):
                self._emit_log(f"[提醒] 指定输出未生成，改用搜索到的最新结果：{latest}")
                self.finished_json.emit(str(latest))
                return

            if self.out_path.exists():
                self.failed.emit(
                    f"后端已结束，但指定输出不是有效装箱 JSON：{self.out_path}。"
                    "需要根节点包含 pallets 列表。"
                )
            else:
                self.failed.emit(
                    f"后端已结束，但没有生成指定输出 JSON：{self.out_path}。"
                    "请查看底部日志中的后端错误信息。"
                )
        except Exception as exc:
            self.failed.emit(str(exc))


class IndustrialPackingWorkbenchClean(IndustrialPackingWorkbench):
    """V3 UI: direct Excel selection + guaranteed output JSON autoload."""

    def __init__(self, project_dir: Path):
        self.selected_excel_original: Optional[Path] = None
        self.selected_excel_copy: Optional[Path] = None
        self.generated_config_path: Optional[Path] = None
        self.generated_out_path: Optional[Path] = None
        self.last_excel_mode: Optional[str] = None
        super().__init__(project_dir)
        self.setWindowTitle("工业装箱工作台 V3 - 一键装箱 + 结果分析")
        self._write_log("[UI] V3模式：主流程为 选择Excel → 一键装箱；高级算法操作已合并到“算法设置”。")

    # ------------------------------------------------------------------ header
    def _build_header(self) -> QtWidgets.QWidget:
        """Top bar: keep the main workflow obvious and move advanced algorithm actions into one menu."""
        header = QtWidgets.QFrame()
        header.setObjectName("Header")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(10)

        title_box = QtWidgets.QVBoxLayout()
        self.title_label = QtWidgets.QLabel("工业装箱工作台")
        self.title_label.setObjectName("MainTitle")
        self.subtitle_label = QtWidgets.QLabel("一键装箱 · 结果分析 · 托盘切换 · 稳定性评估")
        self.subtitle_label.setObjectName("MainSubtitle")
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        layout.addLayout(title_box, 1)

        self.status_pill = StatusPill("空闲")
        self.status_pill.setToolTip("当前运行状态：空闲 / 运行中 / 已完成 / 失败")
        layout.addWidget(self.status_pill)

        self.btn_excel = QtWidgets.QPushButton("选择Excel")
        self.btn_excel.setObjectName("GhostButton")
        self.btn_excel.setToolTip("选择装箱输入 Excel，并自动生成本次运行配置。")
        self.btn_excel.clicked.connect(self.choose_excel_file)
        layout.addWidget(self.btn_excel)

        self.btn_excel_run = QtWidgets.QPushButton("一键装箱")
        self.btn_excel_run.setObjectName("PrimaryButton")
        self.btn_excel_run.setToolTip("主流程：使用已选择的 Excel 运行算法，完成后自动显示最终结果。")
        self.btn_excel_run.clicked.connect(self.start_excel_packing)
        layout.addWidget(self.btn_excel_run)

        self.btn_algo_settings = QtWidgets.QPushButton("算法设置")
        self.btn_algo_settings.setObjectName("GhostButton")
        self.btn_algo_settings.setToolTip("高级功能：切换算法目录、配置文件，或按当前配置复跑算法。日常使用通常不用点。")
        algo_menu = QtWidgets.QMenu(self.btn_algo_settings)
        self.action_choose_project = algo_menu.addAction("选择算法目录…")
        self.action_choose_project.triggered.connect(self.choose_project_dir)
        self.action_choose_config = algo_menu.addAction("选择配置文件…")
        self.action_choose_config.triggered.connect(self.choose_config_file)
        algo_menu.addSeparator()
        self.action_show_algo_settings = algo_menu.addAction("查看当前设置")
        self.action_show_algo_settings.triggered.connect(self.show_algorithm_settings_info)
        self.action_rerun_config = algo_menu.addAction("按当前配置复跑算法")
        self.action_rerun_config.triggered.connect(self.start_backend_packing)
        self.btn_algo_settings.setMenu(algo_menu)
        layout.addWidget(self.btn_algo_settings)

        # 顶部不再显示“开始装箱”，避免和“一键装箱”混淆。
        # 仍保留一个隐藏按钮属性，兼容父类 on_worker_finished/start_backend_packing 里的启停逻辑。
        self.btn_start_backend = QtWidgets.QPushButton("按配置复跑")
        self.btn_start_backend.setObjectName("GhostButton")
        self.btn_start_backend.setToolTip("高级功能：按当前配置文件直接复跑后端算法。")
        self.btn_start_backend.clicked.connect(self.start_backend_packing)
        self.btn_start_backend.setVisible(False)

        self.btn_stop_backend = QtWidgets.QPushButton("停止")
        self.btn_stop_backend.setObjectName("DangerButton")
        self.btn_stop_backend.setToolTip("停止正在运行的后端算法。")
        self.btn_stop_backend.clicked.connect(self.stop_backend_packing)
        self.btn_stop_backend.setEnabled(False)
        layout.addWidget(self.btn_stop_backend)

        self.btn_load_result = QtWidgets.QPushButton("打开结果文件")
        self.btn_load_result.setObjectName("GhostButton")
        self.btn_load_result.setToolTip("手动选择一个 JSON 装箱结果文件并加载显示。")
        self.btn_load_result.clicked.connect(self.load_json_dialog)
        layout.addWidget(self.btn_load_result)

        self.btn_show_latest = QtWidgets.QPushButton("打开最新结果")
        self.btn_show_latest.setObjectName("GhostButton")
        self.btn_show_latest.setToolTip("读取输出目录中最新的装箱结果，并直接显示三维结果。")
        self.btn_show_latest.clicked.connect(self.open_latest_result)
        layout.addWidget(self.btn_show_latest)

        return header

    def show_algorithm_settings_info(self) -> None:
        """Show current backend path/config in a plain dialog for non-technical users."""
        project = getattr(self, "project_dir", None)
        config = getattr(self, "config_path", None)
        excel = getattr(self, "selected_excel_original", None)
        out_path = getattr(self, "generated_out_path", None)
        msg = (
            "当前算法设置：\n\n"
            f"算法目录：{project}\n"
            f"配置文件：{config}\n"
            f"已选择 Excel：{excel or '尚未选择'}\n"
            f"本次输出：{out_path or '尚未生成'}\n\n"
            "日常使用只需要：选择Excel → 一键装箱。\n"
            "只有更换算法工程或 YAML 参数时，才需要修改这里。"
        )
        QtWidgets.QMessageBox.information(self, "算法设置", msg)

    # ------------------------------------------------------------------ Excel
    def choose_excel_file(self) -> Optional[Path]:
        start_dir = _project_data_dir(self.project_dir)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择装箱输入 Excel",
            str(start_dir if start_dir.exists() else self.project_dir),
            "Excel Files (*.xlsx *.xls);;All Files (*.*)",
        )
        if not path:
            return None
        try:
            original = Path(path).resolve()
            copied = _copy_excel_to_project_data(self.project_dir, original)
            run_mode, sheets, warnings = _read_excel_mode(copied)
            cfg = _write_ui_config(self.project_dir, self.project_dir / DEFAULT_CONFIG_REL, copied, run_mode)

            self.selected_excel_original = original
            self.selected_excel_copy = copied
            self.generated_config_path = cfg
            self.config_path = cfg
            self.last_excel_mode = run_mode

            self.step_data.set_state("done", f"Excel：{original.name} | 模式：{run_mode}")
            self._write_log(f"[UI] 已选择 Excel：{original}")
            self._write_log(f"[UI] 已复制到项目数据目录：{copied}")
            self._write_log(f"[UI] 检测到工作表：{', '.join(sheets)}")
            self._write_log(f"[UI] 运行模式：{run_mode}")
            self._write_log(f"[UI] 已生成临时配置：{cfg}")
            for w in warnings:
                self._write_log(f"[警告] {w}")
            return cfg
        except Exception as exc:
            self.on_backend_failed(f"选择 Excel / 生成临时配置失败：{exc}")
            return None

    def start_excel_packing(self) -> None:
        # 已经通过“选择Excel”选过文件时，直接运行；没有选过时再弹出选择框。
        if self.generated_config_path is None or self.selected_excel_copy is None:
            cfg = self.choose_excel_file()
            if cfg is None:
                return
        else:
            self.config_path = self.generated_config_path
            self._write_log(f"[UI] 使用已选择 Excel：{self.selected_excel_original}")
        self.start_backend_packing()

    # ------------------------------------------------------------------ backend
    def start_backend_packing(self) -> None:
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "提示", "后端装箱正在运行。")
            return
        ensure_runtime_dirs(self.project_dir)
        self.generated_out_path = _make_out_path(self.project_dir)
        self.worker = UiPackingWorker(self.project_dir, self.config_path, self.generated_out_path, self)
        self.worker.log.connect(self._write_log)
        self.worker.started_cmd.connect(lambda cmd: self._write_log(f"[CMD] {cmd}"))
        self.worker.failed.connect(self.on_backend_failed)
        self.worker.finished_json.connect(self.on_backend_finished_json)
        self.worker.finished.connect(self.on_worker_finished)

        self.btn_start_backend.setEnabled(False)
        if hasattr(self, "action_rerun_config"):
            self.action_rerun_config.setEnabled(False)
        if hasattr(self, "btn_algo_settings"):
            self.btn_algo_settings.setEnabled(False)
        if hasattr(self, "btn_excel_run"):
            self.btn_excel_run.setEnabled(False)
        if hasattr(self, "btn_excel"):
            self.btn_excel.setEnabled(False)
        self.btn_stop_backend.setEnabled(True)
        self.btn_stop_backend.setVisible(True)
        self.btn_load.setEnabled(False)
        self.step_run.set_state("active", "后端装箱算法正在运行，完成后会自动显示结果")
        self._set_status("running")
        self._write_log("[UI] 开始后端装箱计算。")
        self._write_log(f"[UI] 使用配置：{self.config_path}")
        self._write_log(f"[UI] 指定输出：{self.generated_out_path}")
        self.worker.start()

    def on_worker_finished(self) -> None:
        super().on_worker_finished()
        if hasattr(self, "action_rerun_config"):
            self.action_rerun_config.setEnabled(True)
        if hasattr(self, "btn_algo_settings"):
            self.btn_algo_settings.setEnabled(True)
        if hasattr(self, "btn_excel_run"):
            self.btn_excel_run.setEnabled(True)
        if hasattr(self, "btn_excel"):
            self.btn_excel.setEnabled(True)
        if hasattr(self, "btn_stop_backend"):
            self.btn_stop_backend.setEnabled(False)
            self.btn_stop_backend.setVisible(True)

    def on_backend_finished_json(self, json_path: str) -> None:
        path = Path(json_path)
        self._write_log(f"[UI] 后端完成，正在自动加载结果：{path}")
        try:
            self.load_json_file(path)
            self.show_final_result()
            self.step_run.set_state("done", "后端完成，已直接显示最终三维结果")
            self.step_result.set_state("done", f"结果文件：{path.name}")
            self._set_status("done")
            self.workspace_tabs.setCurrentIndex(0)
        except Exception as exc:
            self.on_backend_failed(f"加载算法输出 JSON 失败：{exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Industrial Packing Workbench V3 Clean")
    parser.add_argument("--project", default=str(_PROJECT_DIR_DEFAULT), help="zhuangxiang_code project directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Industrial Packing Workbench V3")
    win = IndustrialPackingWorkbenchClean(Path(args.project))
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
