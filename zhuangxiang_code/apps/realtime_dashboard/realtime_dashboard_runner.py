# -*- coding: utf-8 -*-
r"""
实时装箱看板启动器

推荐目录结构：
    E:\research_code\装箱算法_h\
        zhuangxiang_code\                 # 装箱算法源码仓库
            apps\realtime_dashboard\      # 本文件所在位置
            code\run_packing.py           # 原装箱算法入口
            code\config\packing_config.yaml
        .venvs\packing-realtime\          # 独立 Python 虚拟环境，不进入源码仓库
        runtime\packing-realtime\         # 日志、临时文件、导出文件

运行方式：
    tools\windows\start_realtime_dashboard.bat
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PyQt5 import QtCore, QtWidgets

THIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = THIS_DIR.parents[1]  # ...\zhuangxiang_code
WORKSPACE_DIR = PROJECT_DIR.parent  # ...\装箱算法_h
RUNTIME_DIR = WORKSPACE_DIR / "runtime" / "packing-realtime"
LOG_DIR = RUNTIME_DIR / "logs"

if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

try:
    from stability_business_dashboard_json import MainWindow as BaseDashboard
except Exception as exc:
    raise RuntimeError(
        "无法导入 stability_business_dashboard_json.py。\n"
        "请确认它位于 apps\\realtime_dashboard 目录下。"
    ) from exc


DEFAULT_CONFIG_REL = Path(r"code\config\packing_config.yaml")
DEFAULT_RUN_SCRIPT_REL = Path(r"code\run_packing.py")


def safe_text(text: object) -> str:
    return "" if text is None else str(text)


def ensure_runtime_dirs() -> None:
    for p in [RUNTIME_DIR, LOG_DIR, RUNTIME_DIR / "exports", RUNTIME_DIR / "temp"]:
        p.mkdir(parents=True, exist_ok=True)


def find_latest_json(project_dir: Path) -> Optional[Path]:
    """寻找装箱算法最新输出 JSON。优先找 packing_plan_*.json。"""
    candidates: List[Path] = []
    search_roots = [
        project_dir / "output",
        project_dir / "outputs",
        project_dir / "code" / "output",
        project_dir / "code" / "outputs",
        project_dir,
        WORKSPACE_DIR / "runtime" / "packing-realtime" / "exports",
    ]
    patterns = ["packing_plan_*.json", "*packing*.json", "*.json"]

    for root in search_roots:
        if not root.exists():
            continue
        for pat in patterns:
            candidates.extend([p for p in root.rglob(pat) if p.is_file()])

    filtered: List[Path] = []
    for p in candidates:
        low = str(p).lower()
        if "config" in low or "__pycache__" in low or "site-packages" in low:
            continue
        filtered.append(p)

    if not filtered:
        return None
    return max(filtered, key=lambda p: p.stat().st_mtime)


class PackingWorker(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    finished_json = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)
    started_cmd = QtCore.pyqtSignal(str)

    def __init__(self, project_dir: Path, config_path: Path, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.config_path = Path(config_path)
        self.process: Optional[subprocess.Popen] = None
        self._stop_requested = False
        ensure_runtime_dirs()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = LOG_DIR / f"backend_{stamp}.log"

    def stop(self):
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

    def _write_backend_log(self, text: str):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def run(self):
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

            cmd = [
                sys.executable,
                str(run_script),
                "--config",
                str(self.config_path),
            ]
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

            time.sleep(0.3)
            latest = find_latest_json(self.project_dir)
            if latest is None:
                self.failed.emit("算法运行完成，但没有找到输出 JSON。请检查 output / outputs 目录。")
                return
            if latest.stat().st_mtime < before_mtime:
                self.log.emit(f"[提醒] 找到的 JSON 可能不是本次新生成文件：{latest}")

            self.finished_json.emit(str(latest))
        except Exception as exc:
            self.failed.emit(str(exc))


class RealtimeDashboard(BaseDashboard):
    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.config_path = self.project_dir / DEFAULT_CONFIG_REL
        self.worker: Optional[PackingWorker] = None
        ensure_runtime_dirs()
        super().__init__()
        self.setWindowTitle("实时装箱算法看板 - 后端计算 + 前端动态显示")
        self._install_realtime_toolbar()
        self._install_log_dock()
        self._write_log(f"[UI] 工作区目录：{WORKSPACE_DIR}")
        self._write_log(f"[UI] 算法源码目录：{self.project_dir}")
        self._write_log(f"[UI] 运行产物目录：{RUNTIME_DIR}")
        self._write_log(f"[UI] 当前 Python：{sys.executable}")
        self._write_log(f"[UI] 默认配置：{self.config_path}")

    def _install_realtime_toolbar(self):
        tb = QtWidgets.QToolBar("实时装箱控制")
        tb.setMovable(False)
        self.addToolBar(QtCore.Qt.TopToolBarArea, tb)

        self.act_choose_project = QtWidgets.QAction("选择算法目录", self)
        self.act_choose_project.triggered.connect(self.choose_project_dir)
        tb.addAction(self.act_choose_project)

        self.act_choose_config = QtWidgets.QAction("选择配置", self)
        self.act_choose_config.triggered.connect(self.choose_config_file)
        tb.addAction(self.act_choose_config)

        tb.addSeparator()
        self.act_start_backend = QtWidgets.QAction("开始后端装箱", self)
        self.act_start_backend.triggered.connect(self.start_backend_packing)
        tb.addAction(self.act_start_backend)

        self.act_stop_backend = QtWidgets.QAction("停止后端", self)
        self.act_stop_backend.triggered.connect(self.stop_backend_packing)
        self.act_stop_backend.setEnabled(False)
        tb.addAction(self.act_stop_backend)

        tb.addSeparator()
        self.act_open_latest = QtWidgets.QAction("打开最新结果", self)
        self.act_open_latest.triggered.connect(self.open_latest_result)
        tb.addAction(self.act_open_latest)

    def _install_log_dock(self):
        dock = QtWidgets.QDockWidget("后端运行日志", self)
        dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(3000)
        dock.setWidget(self.log_box)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, dock)

    def _write_log(self, text: str):
        self.log_box.appendPlainText(safe_text(text))
        cursor = self.log_box.textCursor()
        cursor.movePosition(cursor.End)
        self.log_box.setTextCursor(cursor)

    def choose_project_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择装箱算法项目目录", str(self.project_dir))
        if not path:
            return
        self.project_dir = Path(path)
        self.config_path = self.project_dir / DEFAULT_CONFIG_REL
        self._write_log(f"[UI] 已切换项目目录：{self.project_dir}")
        self._write_log(f"[UI] 默认配置：{self.config_path}")

    def choose_config_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择装箱算法配置 YAML",
            str(self.config_path.parent if self.config_path else self.project_dir),
            "YAML Files (*.yaml *.yml);;All Files (*.*)",
        )
        if not path:
            return
        self.config_path = Path(path)
        self._write_log(f"[UI] 已选择配置：{self.config_path}")

    def start_backend_packing(self):
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "提示", "后端装箱正在运行。")
            return

        self.worker = PackingWorker(self.project_dir, self.config_path, self)
        self.worker.log.connect(self._write_log)
        self.worker.started_cmd.connect(lambda cmd: self._write_log(f"[CMD] {cmd}"))
        self.worker.failed.connect(self.on_backend_failed)
        self.worker.finished_json.connect(self.on_backend_finished_json)
        self.worker.finished.connect(self.on_worker_finished)

        self.act_start_backend.setEnabled(False)
        self.act_stop_backend.setEnabled(True)
        self.btn_load.setEnabled(False)
        self._write_log("[UI] 开始后端装箱计算，界面保持可操作。")
        self.worker.start()

    def stop_backend_packing(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._write_log("[UI] 正在停止后端装箱进程...")

    def on_worker_finished(self):
        self.act_start_backend.setEnabled(True)
        self.act_stop_backend.setEnabled(False)
        self.btn_load.setEnabled(True)

    def on_backend_failed(self, msg: str):
        self._write_log(f"[错误] {msg}")
        QtWidgets.QMessageBox.critical(self, "后端装箱失败", msg)

    def on_backend_finished_json(self, json_path: str):
        path = Path(json_path)
        self._write_log(f"[UI] 后端装箱完成，自动加载结果：{path}")
        try:
            self.load_json_file(path)
            self.animation_idx = 0
            self.refresh_3d_scene()
            QtCore.QTimer.singleShot(300, self.play_animation)
        except Exception as exc:
            self.on_backend_failed(f"加载算法输出 JSON 失败：{exc}")

    def open_latest_result(self):
        latest = find_latest_json(self.project_dir)
        if latest is None:
            QtWidgets.QMessageBox.warning(self, "没有找到结果", "没有找到 packing_plan_*.json 或其他 JSON 输出。")
            return
        self._write_log(f"[UI] 手动加载最新结果：{latest}")
        self.load_json_file(latest)
        self.animation_idx = 0
        self.refresh_3d_scene()
        self.play_animation()

    def closeEvent(self, event):
        try:
            self.stop_backend_packing()
        except Exception:
            pass
        super().closeEvent(event)


def parse_args():
    parser = argparse.ArgumentParser(description="实时装箱看板")
    parser.add_argument("--project", default=str(PROJECT_DIR), help="装箱算法项目目录")
    return parser.parse_args()


def main():
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv)
    win = RealtimeDashboard(Path(args.project))
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
