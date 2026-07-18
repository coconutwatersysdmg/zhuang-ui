# -*- coding: utf-8 -*-
"""
业务层 JSON + 稳定性评价一体化看板

功能：
1. 直接读取装箱算法导出的 packing_plan_*.json；
2. 按托盘显示箱子信息表、3D 摆放可视化、吸盘吸附区域；
3. 自动计算稳定性指标：重心高度、重心偏移、支撑充分性、承压安全、抗倾覆、抗滑移、综合评分；
4. 支持按重量 / 支撑风险 / 层高 / 箱型 / 吸盘规格着色；
5. 支持按装箱顺序播放，并联动表格、风险清单和右侧详情卡片。

依赖：PyQt5、numpy、pandas、pyqtgraph、PyOpenGL
运行：python stability_business_dashboard_json.py
"""

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import pyqtgraph.opengl as gl
    HAS_GL = True
except Exception:
    gl = None
    HAS_GL = False


# ============================================================
# 基础计算工具
# ============================================================

REQUIRED_BASE_COLUMNS = ["box_id", "mass", "x", "y", "z", "lx", "ly", "lz"]
VALID_CORNER_NAMES = {"x_min_y_min", "x_max_y_min", "x_min_y_max", "x_max_y_max"}

# 2026-07 调整：当前业务 case 暂不考虑箱体承压约束。
# 公司侧建议保留 max_load 字段，但在缺失/无效时给一个足够大的数，等价于“不限制承压”。
# 后续如果公司提供真实 max_load，只要 JSON/Excel 里有正数 max_load，会自动使用真实值。
UNLIMITED_MAX_LOAD_KG = 1_000_000_000.0


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def safe_str(v: Any, default: str = "--") -> str:
    if v is None:
        return default
    text = str(v).strip()
    return text if text else default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def overlap_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def overlap_area_xy(upper: pd.Series, lower: pd.Series) -> float:
    dx = overlap_1d(upper["x"], upper["x"] + upper["lx"], lower["x"], lower["x"] + lower["lx"])
    dy = overlap_1d(upper["y"], upper["y"] + upper["ly"], lower["y"], lower["y"] + lower["ly"])
    return float(dx * dy)


def to_numeric(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def compute_support_and_load(df: pd.DataFrame, z_tol: float = 2.0, floor_tol: float = 2.0) -> pd.DataFrame:
    """根据箱体真实接触关系补算支撑面积、支撑率和向下传递载荷。单位：mm / kg。"""
    result = df.copy().reset_index(drop=True)
    n = len(result)
    result["base_area"] = result["lx"] * result["ly"]
    result["support_area_calc"] = 0.0
    result["support_ratio_calc"] = 0.0
    result["load_above"] = 0.0
    result["direct_support_count"] = 0
    result["direct_supported_by_count"] = 0

    supports_map: Dict[int, List[Tuple[int, float]]] = {i: [] for i in range(n)}
    supported_by_map: Dict[int, List[int]] = {i: [] for i in range(n)}

    for ui in range(n):
        upper = result.loc[ui]

        if abs(float(upper["z"])) <= floor_tol:
            result.at[ui, "support_area_calc"] = float(result.at[ui, "base_area"])
            result.at[ui, "support_ratio_calc"] = 1.0
            continue

        top_supports = []
        for li in range(n):
            if li == ui:
                continue
            lower = result.loc[li]
            lower_top_z = float(lower["z"] + lower["lz"])
            if abs(float(upper["z"]) - lower_top_z) > z_tol:
                continue
            area = overlap_area_xy(upper, lower)
            if area > 0:
                top_supports.append((li, area))

        supports_map[ui] = top_supports
        if top_supports:
            support_area = float(sum(a for _, a in top_supports))
            base_area = max(float(result.at[ui, "base_area"]), 1e-9)
            result.at[ui, "support_area_calc"] = support_area
            result.at[ui, "support_ratio_calc"] = clamp(support_area / base_area, 0.0, 1.0)
            result.at[ui, "direct_supported_by_count"] = len(top_supports)
            for li, _ in top_supports:
                supported_by_map[li].append(ui)

    for li in range(n):
        result.at[li, "direct_support_count"] = len(supported_by_map[li])

    fractions_map: Dict[int, List[Tuple[int, float]]] = {i: [] for i in range(n)}
    for ui, overlaps in supports_map.items():
        total_area = float(sum(a for _, a in overlaps))
        if total_area > 0:
            fractions_map[ui] = [(li, a / total_area) for li, a in overlaps]

    order = list(result.sort_values(["z", "y", "x"], ascending=[False, True, True]).index)
    for ui in order:
        downward = float(result.at[ui, "mass"]) + float(result.at[ui, "load_above"])
        for li, alpha in fractions_map[ui]:
            result.at[li, "load_above"] += downward * alpha

    # 承压字段处理：当前 case 暂不限制承压。
    # 如果 max_load 缺失、为空、<=0，则自动填入一个超大值；若后续提供真实 max_load，则保持真实值。
    if "max_load" not in result.columns:
        result["max_load"] = UNLIMITED_MAX_LOAD_KG
        result["max_load_unlimited"] = True
    else:
        invalid_max_load = result["max_load"].isna() | (result["max_load"] <= 0)
        result["max_load_unlimited"] = invalid_max_load
        result.loc[invalid_max_load, "max_load"] = UNLIMITED_MAX_LOAD_KG

    result["pressure_utilization"] = result["load_above"] / result["max_load"]

    # 主界面默认使用重新计算值；JSON 原始值仍保留在 support_ratio_json / supported_area_json 中。
    result["support_area"] = result["support_area_calc"]
    result["support_ratio"] = result["support_ratio_calc"]
    return result


def compute_scores(df: pd.DataFrame, L: float, W: float, H: float, ax_g: float = 0.4, ay_g: float = 0.3, friction: float = 0.45) -> Dict[str, Any]:
    """输出稳定性评价指标。L/W/H 单位 mm。"""
    out: Dict[str, Any] = {}
    masses = df["mass"].to_numpy(dtype=float)
    total_mass = float(masses.sum())
    if total_mass <= 0:
        raise ValueError("总质量必须大于 0，无法计算稳定性。")

    cx = (df["x"] + df["lx"] / 2.0).to_numpy(dtype=float)
    cy = (df["y"] + df["ly"] / 2.0).to_numpy(dtype=float)
    cz = (df["z"] + df["lz"] / 2.0).to_numpy(dtype=float)

    x_c = float((masses * cx).sum() / total_mass)
    y_c = float((masses * cy).sum() / total_mass)
    z_c = float((masses * cz).sum() / total_mass)
    out.update({"cg_x": x_c, "cg_y": y_c, "cg_z": z_c, "total_mass": total_mass})

    z_ratio = z_c / max(H, 1e-9)
    if z_ratio <= 0.20:
        s_cgh = 100.0
    elif z_ratio >= 0.70:
        s_cgh = 0.0
    else:
        s_cgh = 100.0 * (0.70 - z_ratio) / (0.70 - 0.20)
    out["cg_height"] = (s_cgh, z_ratio)

    pallet_center_x, pallet_center_y = L / 2.0, W / 2.0
    d = math.hypot(x_c - pallet_center_x, y_c - pallet_center_y)
    d_max = math.hypot(L / 2.0, W / 2.0)
    d_ratio = d / max(d_max, 1e-9)
    if d_ratio <= 0.05:
        s_cgo = 100.0
    elif d_ratio >= 0.50:
        s_cgo = 0.0
    else:
        s_cgo = 100.0 * (0.50 - d_ratio) / (0.50 - 0.05)
    out["cg_offset"] = (s_cgo, d_ratio)

    r = df["support_ratio"].fillna(0).to_numpy(dtype=float)
    r_bar = float((masses * r).sum() / total_mass)
    if r_bar <= 0.60:
        s_sup = 0.0
    elif r_bar >= 0.95:
        s_sup = 100.0
    else:
        s_sup = 100.0 * (r_bar - 0.60) / (0.95 - 0.60)
    out["support"] = (s_sup, r_bar)

    if "pressure_utilization" in df.columns and df["pressure_utilization"].notna().any():
        u = df["pressure_utilization"].replace([np.inf, -np.inf], np.nan).dropna()
        u_max = float(u.max()) if len(u) else np.nan
        if not np.isnan(u_max):
            if u_max <= 0.50:
                s_press = 100.0
            elif u_max >= 1.20:
                s_press = 0.0
            else:
                s_press = 100.0 * (1.20 - u_max) / (1.20 - 0.50)
            out["pressure"] = (s_press, u_max)
        else:
            out["pressure"] = None
    else:
        out["pressure"] = None

    bx = min(x_c, L - x_c)
    by = min(y_c, W - y_c)
    Kx = bx / max(ax_g * z_c, 1e-9)
    Ky = by / max(ay_g * z_c, 1e-9)
    K_tip = min(Kx, Ky)
    if K_tip <= 0.90:
        s_tip = 0.0
    elif K_tip >= 2.00:
        s_tip = 100.0
    else:
        s_tip = 100.0 * (K_tip - 0.90) / (2.00 - 0.90)
    out["tip"] = (s_tip, K_tip)

    a = max(ax_g, ay_g)
    K_slide = friction / max(a, 1e-9)
    if K_slide <= 0.90:
        s_slide = 0.0
    elif K_slide >= 2.00:
        s_slide = 100.0
    else:
        s_slide = 100.0 * (K_slide - 0.90) / (2.00 - 0.90)
    out["slide"] = (s_slide, K_slide)

    max_height = float((df["z"] + df["lz"]).max()) if len(df) else 0.0
    out["height_utilization"] = max_height / max(H, 1e-9)
    out["volume_utilization"] = float((df["lx"] * df["ly"] * df["lz"]).sum()) / max(L * W * H, 1e-9)
    out["layer_count"] = int(len(sorted(set(np.round(df["z"].to_numpy(dtype=float), 3)))))

    weights = {
        "cg_height": 0.14,
        "cg_offset": 0.18,
        "support": 0.24,
        "pressure": 0.16,
        "tip": 0.18,
        "slide": 0.10,
    }
    total_w = 0.0
    total_s = 0.0
    for key, w in weights.items():
        val = out.get(key)
        if val is None:
            continue
        total_s += float(val[0]) * w
        total_w += w
    out["total_score"] = total_s / max(total_w, 1e-9)
    return out


def score_status(score: float) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 60:
        return "一般"
    return "风险"


def score_level(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def blue_red_rgba(ratio: float, alpha: float = 0.86) -> Tuple[float, float, float, float]:
    ratio = clamp(ratio, 0.0, 1.0)
    r = ratio
    g = 0.12 + 0.28 * (1.0 - abs(ratio - 0.5) * 2.0)
    b = 1.0 - ratio
    return (r, g, b, alpha)


def categorical_rgba(text: Any, alpha: float = 0.86) -> Tuple[float, float, float, float]:
    h = (abs(hash(str(text))) % 360) / 360.0
    q = QtGui.QColor.fromHsvF(h, 0.60, 0.92, alpha)
    return (q.redF(), q.greenF(), q.blueF(), alpha)


def fmt_num(v: Any, nd: int = 2, suffix: str = "") -> str:
    try:
        x = float(v)
        if np.isnan(x):
            return "--"
        return f"{x:.{nd}f}{suffix}"
    except Exception:
        return "--"


def build_cube_meshdata():
    """构建一个以左下前底点为原点、尺寸为 1 的立方体。"""
    verts = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=float)
    faces = np.array([
        [0, 1, 2], [0, 2, 3],
        [4, 6, 5], [4, 7, 6],
        [0, 4, 5], [0, 5, 1],
        [1, 5, 6], [1, 6, 2],
        [2, 6, 7], [2, 7, 3],
        [3, 7, 4], [3, 4, 0],
    ], dtype=int)
    return gl.MeshData(vertexes=verts, faces=faces)


# ============================================================
# 控件
# ============================================================

class MetricCard(QtWidgets.QFrame):
    def __init__(self, title: str, value: str = "--", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("MetricCard")
        self.setMinimumHeight(86)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setObjectName("MetricTitle")
        self.value_label = QtWidgets.QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.subtitle_label = QtWidgets.QLabel(subtitle)
        self.subtitle_label.setObjectName("MetricSub")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.subtitle_label)

    def set_data(self, value: str, subtitle: str = "", state: str = "normal"):
        self.value_label.setText(value)
        self.subtitle_label.setText(subtitle)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class ColorBarWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = "数值"
        self.vmin = 0.0
        self.vmax = 1.0
        self.setMinimumWidth(90)
        self.setMaximumWidth(105)

    def set_info(self, title: str, vmin: float, vmax: float):
        self.title = title
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.rect().adjusted(10, 12, -10, -12)
        painter.setPen(QtGui.QColor("#111827"))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRect(rect.left(), rect.top(), rect.width(), 24), QtCore.Qt.AlignCenter, self.title)

        bar_rect = QtCore.QRect(rect.left() + 24, rect.top() + 34, 18, max(90, rect.height() - 62))
        grad = QtGui.QLinearGradient(bar_rect.topLeft(), bar_rect.bottomLeft())
        grad.setColorAt(0.0, QtGui.QColor(220, 38, 38))
        grad.setColorAt(1.0, QtGui.QColor(37, 99, 235))
        painter.fillRect(bar_rect, grad)
        painter.setPen(QtGui.QColor("#4b5563"))
        painter.drawRoundedRect(bar_rect, 4, 4)

        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(bar_rect.right() + 6, bar_rect.top() + 6, f"{self.vmax:.3g}")
        painter.drawText(bar_rect.right() + 6, bar_rect.bottom(), f"{self.vmin:.3g}")


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("装箱业务层 - 稳定性评价与箱子可视化看板")
        self.resize(1780, 1020)

        self.plan_data: Optional[Dict[str, Any]] = None
        self.pallets: List[Dict[str, Any]] = []
        self.filtered_pallets: List[Dict[str, Any]] = []
        self.current_pallet: Optional[Dict[str, Any]] = None
        self.df_raw: Optional[pd.DataFrame] = None
        self.df_eval: Optional[pd.DataFrame] = None
        self.score_result: Optional[Dict[str, Any]] = None
        self.current_path: Optional[Path] = None
        self.animation_order: List[int] = []
        self.animation_idx = 0
        self.mesh_items: List[Any] = []
        self.scene_items: List[Any] = []
        self.selected_row_index: Optional[int] = None
        self.mesh_box_template = build_cube_meshdata() if HAS_GL else None

        self.anim_timer = QtCore.QTimer(self)
        self.anim_timer.timeout.connect(self._anim_step)

        self._build_ui()
        self._apply_style()

    # ------------------------- UI -------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        top_bar = QtWidgets.QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QtWidgets.QHBoxLayout(top_bar)
        top_layout.setContentsMargins(18, 12, 18, 12)
        top_layout.setSpacing(12)

        title_box = QtWidgets.QVBoxLayout()
        self.title_label = QtWidgets.QLabel("装箱业务层 · 稳定性评价与箱子可视化看板")
        self.title_label.setObjectName("TitleLabel")
        self.subtitle_label = QtWidgets.QLabel("JSON 输入 / 箱子明细 / 吸盘信息 / 3D 摆放 / 稳定性指标 / 风险预警")
        self.subtitle_label.setObjectName("SubtitleLabel")
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        top_layout.addLayout(title_box, 1)

        self.btn_load = QtWidgets.QPushButton("加载装箱 JSON")
        self.btn_load.clicked.connect(self.load_json_dialog)
        self.btn_export = QtWidgets.QPushButton("导出当前评价表")
        self.btn_export.clicked.connect(self.export_eval_table)
        self.btn_export.setEnabled(False)
        self.btn_recalc = QtWidgets.QPushButton("重新计算稳定性")
        self.btn_recalc.clicked.connect(self.recalculate_current)
        self.btn_recalc.setEnabled(False)
        top_layout.addWidget(self.btn_load)
        top_layout.addWidget(self.btn_recalc)
        top_layout.addWidget(self.btn_export)
        root_layout.addWidget(top_bar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        left_panel = self._build_left_panel()
        center_panel = self._build_center_panel()
        right_panel = self._build_right_panel()
        splitter.addWidget(left_panel)
        splitter.addWidget(center_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([360, 980, 430])

    def _build_left_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("SidePanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 10, 14)
        layout.setSpacing(10)

        g_file = QtWidgets.QGroupBox("1. 文件与计划信息")
        glay = QtWidgets.QVBoxLayout(g_file)
        self.file_info = QtWidgets.QLabel("尚未加载文件")
        self.file_info.setWordWrap(True)
        self.file_info.setObjectName("InfoText")
        glay.addWidget(self.file_info)
        layout.addWidget(g_file)

        g_overview = QtWidgets.QGroupBox("2. 计划总体概览")
        overview_layout = QtWidgets.QGridLayout(g_overview)
        self.card_total = MetricCard("总托盘数")
        self.card_success = MetricCard("成功托盘")
        self.card_failed = MetricCard("失败托盘")
        self.card_gap = MetricCard("平均缺口")
        overview_layout.addWidget(self.card_total, 0, 0)
        overview_layout.addWidget(self.card_success, 0, 1)
        overview_layout.addWidget(self.card_failed, 1, 0)
        overview_layout.addWidget(self.card_gap, 1, 1)
        layout.addWidget(g_overview)

        g_select = QtWidgets.QGroupBox("3. 托盘筛选与选择")
        form = QtWidgets.QFormLayout(g_select)
        self.cmb_status = QtWidgets.QComboBox()
        self.cmb_status.addItems(["全部", "SUCCESS", "FAILED", "UNKNOWN"])
        self.cmb_status.currentIndexChanged.connect(self.refresh_pallet_filter)
        self.cmb_order = QtWidgets.QComboBox()
        self.cmb_order.addItem("全部订单")
        self.cmb_order.currentIndexChanged.connect(self.refresh_pallet_filter)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("输入托盘ID / 箱型 / 订单号搜索")
        self.search_edit.textChanged.connect(self.refresh_pallet_filter)
        self.cmb_pallet = QtWidgets.QComboBox()
        self.cmb_pallet.currentIndexChanged.connect(self.on_pallet_changed)
        self.lbl_filter_count = QtWidgets.QLabel("可选托盘：0")
        self.lbl_filter_count.setObjectName("InfoText")
        form.addRow("状态", self.cmb_status)
        form.addRow("订单", self.cmb_order)
        form.addRow("搜索", self.search_edit)
        form.addRow("托盘", self.cmb_pallet)
        form.addRow("结果", self.lbl_filter_count)
        layout.addWidget(g_select)

        g_param = QtWidgets.QGroupBox("4. 稳定性计算参数")
        pform = QtWidgets.QFormLayout(g_param)
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
        pform.addRow("接触Z容差", self.sp_z_tol)
        pform.addRow("X向加速度/g", self.sp_ax)
        pform.addRow("Y向加速度/g", self.sp_ay)
        pform.addRow("摩擦系数μ", self.sp_mu)
        layout.addWidget(g_param)

        g_hint = QtWidgets.QGroupBox("5. 界面说明")
        hint = QtWidgets.QPlainTextEdit()
        hint.setReadOnly(True)
        hint.setMaximumHeight(190)
        hint.setPlainText(
            "输入格式：装箱算法 JSON，根节点包含 summary 和 pallets。\n\n"
            "每个托盘读取：pallet_id、mpm_total、mpm_gap、mpm_status、packed_items。\n\n"
            "每个箱子读取：id、type、length、width、height、weight、position、support_ratio、suction_*、pallet_dims。\n\n"
            "表格红色表示高风险，黄色表示轻微预警；3D 中红点为整垛质心。"
        )
        hv = QtWidgets.QVBoxLayout(g_hint)
        hv.addWidget(hint)
        layout.addWidget(g_hint)
        layout.addStretch(1)
        return panel

    def _build_center_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("CenterPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(8, 14, 8, 14)
        layout.setSpacing(10)

        g_vis = QtWidgets.QGroupBox("箱垛三维可视化")
        vis_layout = QtWidgets.QVBoxLayout(g_vis)
        ctrl = QtWidgets.QHBoxLayout()
        self.btn_show_final = QtWidgets.QPushButton("显示结果")
        self.btn_show_final.clicked.connect(self.show_final_result)
        self.btn_play = QtWidgets.QPushButton("动态演示")
        self.btn_play.clicked.connect(self.play_animation)
        self.btn_pause = QtWidgets.QPushButton("暂停")
        self.btn_pause.clicked.connect(self.pause_animation)
        self.btn_reset = QtWidgets.QPushButton("重置")
        self.btn_reset.clicked.connect(self.reset_animation)
        self.cmb_color = QtWidgets.QComboBox()
        self.cmb_color.addItems(["按支撑风险着色", "按重量着色", "按层高着色", "按箱型着色", "按吸盘规格着色"])
        self.cmb_color.currentIndexChanged.connect(self.refresh_3d_scene)
        self.chk_suction = QtWidgets.QCheckBox("吸盘")
        self.chk_suction.setChecked(True)
        self.chk_suction.stateChanged.connect(self.refresh_3d_scene)
        self.chk_risk_outline = QtWidgets.QCheckBox("高亮")
        self.chk_risk_outline.setChecked(True)
        self.chk_risk_outline.stateChanged.connect(self.refresh_3d_scene)
        self.chk_cg_projection = QtWidgets.QCheckBox("重心")
        self.chk_cg_projection.setChecked(True)
        self.chk_cg_projection.stateChanged.connect(self.refresh_3d_scene)
        self.chk_only_risk = QtWidgets.QCheckBox("风险箱")
        self.chk_only_risk.stateChanged.connect(self.refresh_3d_scene)
        self.anim_label = QtWidgets.QLabel("进度：0 / 0")
        ctrl.addWidget(self.btn_show_final)
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.btn_pause)
        ctrl.addWidget(self.btn_reset)
        ctrl.addSpacing(12)
        ctrl.addWidget(QtWidgets.QLabel("着色"))
        ctrl.addWidget(self.cmb_color)
        ctrl.addWidget(self.chk_suction)
        ctrl.addWidget(self.chk_risk_outline)
        ctrl.addWidget(self.chk_cg_projection)
        ctrl.addWidget(self.chk_only_risk)
        ctrl.addSpacing(10)
        for text, mode in [("俯视", "top"), ("正视", "front"), ("侧视", "side"), ("立体", "iso")]:
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _, m=mode: self.set_view_preset(m))
            ctrl.addWidget(b)
        ctrl.addStretch(1)
        ctrl.addWidget(self.anim_label)
        vis_layout.addLayout(ctrl)

        vis_wrap = QtWidgets.QHBoxLayout()
        if HAS_GL:
            self.view3d = gl.GLViewWidget()
            self.view3d.setBackgroundColor("#ffffff")
            self.view3d.opts["distance"] = 3900
            self.view3d.opts["elevation"] = 25
            self.view3d.opts["azimuth"] = -58
            vis_wrap.addWidget(self.view3d, 1)
        else:
            self.view3d = QtWidgets.QLabel("当前环境缺少 pyqtgraph.opengl / PyOpenGL，无法显示 3D。")
            self.view3d.setAlignment(QtCore.Qt.AlignCenter)
            vis_wrap.addWidget(self.view3d, 1)
        self.colorbar = ColorBarWidget()
        vis_wrap.addWidget(self.colorbar)
        vis_layout.addLayout(vis_wrap, 1)
        layout.addWidget(g_vis, 3)

        g_table = QtWidgets.QGroupBox("箱子任务明细表（业务层字段 + 稳定性补算字段）")
        table_layout = QtWidgets.QVBoxLayout(g_table)
        self.box_table = QtWidgets.QTableWidget()
        self.box_table.setColumnCount(19)
        self.box_table.setHorizontalHeaderLabels([
            "原序", "机器序", "箱号", "类型", "长×宽×高(mm)", "重量(kg)", "X", "Y", "Z", "层号",
            "支撑率", "支撑面积", "承压利用", "吸附箱角", "吸盘角", "吸盘规格", "吸附矩形", "MPM", "风险"
        ])
        self.box_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.box_table.horizontalHeader().setStretchLastSection(True)
        self.box_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.box_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.box_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.box_table.setAlternatingRowColors(True)
        self.box_table.itemSelectionChanged.connect(self.on_table_selection_changed)
        table_layout.addWidget(self.box_table)
        layout.addWidget(g_table, 2)
        return panel

    def _build_right_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setObjectName("RightPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(10, 14, 14, 14)
        layout.setSpacing(10)

        g_pallet = QtWidgets.QGroupBox("当前托盘核心指标")
        grid = QtWidgets.QGridLayout(g_pallet)
        self.card_score = MetricCard("综合评分")
        self.card_level = MetricCard("稳定等级")
        self.card_boxes = MetricCard("箱子数量")
        self.card_mass = MetricCard("总重量")
        self.card_height = MetricCard("高度利用率")
        self.card_support = MetricCard("平均支撑率")
        self.card_cg = MetricCard("重心偏移")
        self.card_mpm = MetricCard("MPM状态")
        cards = [self.card_score, self.card_level, self.card_boxes, self.card_mass, self.card_height, self.card_support, self.card_cg, self.card_mpm]
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 2, i % 2)
        layout.addWidget(g_pallet)

        g_score = QtWidgets.QGroupBox("稳定性分项评分")
        score_layout = QtWidgets.QVBoxLayout(g_score)
        self.score_table = QtWidgets.QTableWidget()
        self.score_table.setColumnCount(5)
        self.score_table.setHorizontalHeaderLabels(["指标", "分数", "当前值", "状态", "说明"])
        self.score_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.score_table.horizontalHeader().setStretchLastSection(True)
        self.score_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.score_table.setAlternatingRowColors(True)
        score_layout.addWidget(self.score_table)
        layout.addWidget(g_score, 2)

        g_warning = QtWidgets.QGroupBox("风险预警清单")
        warning_layout = QtWidgets.QVBoxLayout(g_warning)
        self.warning_list = QtWidgets.QListWidget()
        warning_layout.addWidget(self.warning_list)
        layout.addWidget(g_warning, 2)

        g_detail = QtWidgets.QGroupBox("选中箱子详情")
        detail_layout = QtWidgets.QVBoxLayout(g_detail)
        self.box_detail = QtWidgets.QPlainTextEdit()
        self.box_detail.setReadOnly(True)
        self.box_detail.setPlainText("在表格中选择一个箱子后显示详细信息。")
        detail_layout.addWidget(self.box_detail)
        layout.addWidget(g_detail, 2)
        return panel

    def _apply_style(self):
        self.setStyleSheet("""
        * {
            font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", Arial;
            font-size: 13px;
        }
        QMainWindow, QWidget#SidePanel, QWidget#CenterPanel, QWidget#RightPanel {
            background: #f4f6fb;
        }
        QFrame#TopBar {
            background: #111827;
        }
        QLabel#TitleLabel {
            color: #ffffff;
            font-size: 22px;
            font-weight: 800;
        }
        QLabel#SubtitleLabel {
            color: #cbd5e1;
            font-size: 12px;
        }
        QLabel#InfoText {
            color: #4b5563;
            line-height: 150%;
        }
        QPushButton {
            background: #b91c1c;
            color: white;
            border: none;
            border-radius: 7px;
            padding: 7px 13px;
            font-weight: 700;
        }
        QPushButton:hover {
            background: #dc2626;
        }
        QPushButton:disabled {
            background: #d1d5db;
            color: #6b7280;
        }
        QGroupBox {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 11px;
            margin-top: 18px;
            padding: 10px;
            font-weight: 800;
            color: #111827;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
            padding: 0 6px;
            color: #7f1d1d;
            background: #ffffff;
        }
        QFrame#MetricCard {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 10px;
        }
        QFrame#MetricCard[state="good"] {
            border: 1px solid #86efac;
            background: #f0fdf4;
        }
        QFrame#MetricCard[state="warn"] {
            border: 1px solid #fde68a;
            background: #fffbeb;
        }
        QFrame#MetricCard[state="bad"] {
            border: 1px solid #fecaca;
            background: #fef2f2;
        }
        QLabel#MetricTitle {
            color: #6b7280;
            font-size: 12px;
            font-weight: 700;
        }
        QLabel#MetricValue {
            color: #111827;
            font-size: 20px;
            font-weight: 900;
        }
        QLabel#MetricSub {
            color: #6b7280;
            font-size: 11px;
        }
        QTableWidget {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            gridline-color: #e5e7eb;
            alternate-background-color: #f9fafb;
            selection-background-color: #ffe4e6;
            selection-color: #111827;
        }
        QHeaderView::section {
            background: #fee2e2;
            color: #7f1d1d;
            font-weight: 800;
            padding: 6px;
            border: 1px solid #fecaca;
        }
        QComboBox, QLineEdit, QDoubleSpinBox, QPlainTextEdit, QListWidget {
            background: #ffffff;
            border: 1px solid #d1d5db;
            border-radius: 7px;
            padding: 5px;
        }
        QPlainTextEdit {
            color: #374151;
        }
        QSplitter::handle {
            background: #e5e7eb;
        }
        """)

    # ------------------------- 数据读取 -------------------------
    def load_json_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择装箱算法 JSON",
            str(Path.cwd()),
            "JSON Files (*.json);;All Files (*.*)",
        )
        if path:
            self.load_json_file(Path(path))

    def load_json_file(self, path: Path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("pallets"), list):
                raise ValueError("该文件不是预期的装箱规划 JSON：根节点需要包含 pallets 列表。")
            self.plan_data = data
            self.pallets = list(data.get("pallets", []))
            self.current_path = path
            self.populate_after_load()
        except Exception as e:
            self.show_error(f"加载 JSON 失败：{e}")

    def populate_after_load(self):
        if self.plan_data is None:
            return
        summary = self.plan_data.get("summary", {}).get("overall", {})
        plan_id = safe_str(self.plan_data.get("packing_plan_id"))
        runtime = safe_float(self.plan_data.get("total_runtime_seconds"), 0.0)
        self.file_info.setText(
            f"已加载：{self.current_path}\n"
            f"计划ID：{plan_id}\n"
            f"托盘数量：{len(self.pallets)}\n"
            f"总运行时间：{runtime:.2f} s"
        )

        total = int(summary.get("total_pallets", len(self.pallets)) or len(self.pallets))
        succ = int(summary.get("success_pallets", 0) or 0)
        fail = int(summary.get("failed_pallets", 0) or 0)
        gap = safe_float(summary.get("avg_mpm_gap"), 0.0)
        self.card_total.set_data(str(total), "全部托盘")
        self.card_success.set_data(str(succ), f"成功率 {succ / max(total, 1) * 100:.1f}%", "good")
        self.card_failed.set_data(str(fail), "需要重点复核", "bad" if fail else "good")
        self.card_gap.set_data(f"{gap:.2f}", "平均 MPM 缺口", "warn" if gap > 0 else "good")

        self.cmb_order.blockSignals(True)
        self.cmb_order.clear()
        self.cmb_order.addItem("全部订单")
        orders = sorted({safe_str(p.get("sales_order_no")) for p in self.pallets if p.get("sales_order_no")})
        self.cmb_order.addItems(orders)
        self.cmb_order.blockSignals(False)
        self.refresh_pallet_filter()
        self.fill_failed_table()
        self.btn_recalc.setEnabled(True)
        self.btn_export.setEnabled(True)

    def refresh_pallet_filter(self):
        status = self.cmb_status.currentText() if hasattr(self, "cmb_status") else "全部"
        order = self.cmb_order.currentText() if hasattr(self, "cmb_order") else "全部订单"
        keyword = self.search_edit.text().strip().lower() if hasattr(self, "search_edit") else ""

        filtered = []
        for p in self.pallets:
            p_status = safe_str(p.get("mpm_status"), "UNKNOWN")
            p_order = safe_str(p.get("sales_order_no"), "--")
            p_id = safe_str(p.get("pallet_id"), "--")
            items = p.get("packed_items", []) or []
            type_text = " ".join(sorted({safe_str(it.get("type"), "") for it in items}))
            hay = f"{p_id} {p_order} {type_text}".lower()
            if status != "全部" and p_status != status:
                continue
            if order != "全部订单" and p_order != order:
                continue
            if keyword and keyword not in hay:
                continue
            filtered.append(p)
        self.filtered_pallets = filtered

        self.cmb_pallet.blockSignals(True)
        self.cmb_pallet.clear()
        for p in filtered:
            p_id = safe_str(p.get("pallet_id"))
            p_status = safe_str(p.get("mpm_status"), "UNKNOWN")
            count = len(p.get("packed_items", []) or [])
            gap = safe_float(p.get("mpm_gap"), 0.0)
            self.cmb_pallet.addItem(f"{p_id}  |  {p_status}  |  {count}箱  |  gap={gap:g}")
        self.cmb_pallet.blockSignals(False)
        self.lbl_filter_count.setText(f"可选托盘：{len(filtered)}")
        if filtered:
            self.cmb_pallet.setCurrentIndex(0)
            self.load_pallet(filtered[0])
        else:
            self.current_pallet = None
            self.clear_current_views()

    def focus_pallet_by_id(self, pallet_id: str) -> None:
        """从失败/风险列表跳转到指定托盘。"""
        pallet_id = safe_str(pallet_id, "").strip()
        if not pallet_id or not hasattr(self, "cmb_pallet"):
            return
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
            for i, p in enumerate(self.filtered_pallets):
                if safe_str(p.get("pallet_id"), "") == pallet_id:
                    self.cmb_pallet.setCurrentIndex(i)
                    self.load_pallet(p)
                    if hasattr(self, "workspace_tabs"):
                        self.workspace_tabs.setCurrentIndex(0)
                    return
        except Exception:
            pass

    def _collect_item_failures(self, container: Any, pallet_id: str = "--", order_no: str = "--") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not isinstance(container, dict):
            return rows
        for key in ["failed_items", "unpacked_items", "unplaced_items", "remaining_items", "failed_boxes", "unpacked_boxes"]:
            items = container.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                box_id = safe_str(item.get("id"), safe_str(item.get("box_id"), "--"))
                box_type = safe_str(item.get("type"), safe_str(item.get("包装规格代码"), "--"))
                reason = safe_str(item.get("reason"), safe_str(item.get("failure_reason"), key))
                rows.append({
                    "kind": "失败箱",
                    "pallet_id": pallet_id,
                    "order_no": safe_str(item.get("sales_order_no"), order_no),
                    "status": key,
                    "box_id": box_id,
                    "box_type": box_type,
                    "count": safe_str(item.get("count"), "1"),
                    "gap": "--",
                    "fill_rate": "--",
                    "reason": reason,
                })
        return rows

    def extract_failure_rows(self) -> List[Dict[str, Any]]:
        """提取失败箱/失败托盘信息。当前算法若没有逐箱失败字段，则显示 FAILED 托盘和低装载原因。"""
        rows: List[Dict[str, Any]] = []
        if self.plan_data is None:
            return rows
        rows.extend(self._collect_item_failures(self.plan_data))
        for p in self.pallets:
            pallet_id = safe_str(p.get("pallet_id"), "--")
            order_no = safe_str(p.get("sales_order_no"), "--")
            rows.extend(self._collect_item_failures(p, pallet_id=pallet_id, order_no=order_no))
            status = safe_str(p.get("mpm_status"), "UNKNOWN")
            gap = safe_float(p.get("mpm_gap"), 0.0)
            fill_rate = safe_float(p.get("fill_rate"), np.nan)
            if status != "SUCCESS" or gap > 0:
                reasons = []
                if status != "SUCCESS":
                    reasons.append(f"MPM状态={status}")
                if gap > 0:
                    reasons.append(f"MPM缺口={gap:g}")
                if not np.isnan(fill_rate) and fill_rate < 0.70:
                    reasons.append(f"填充率偏低={fill_rate * 100:.1f}%")
                rows.append({
                    "kind": "失败托盘",
                    "pallet_id": pallet_id,
                    "order_no": order_no,
                    "status": status,
                    "box_id": "--",
                    "box_type": safe_str(p.get("pallet_type"), "--"),
                    "count": str(len(p.get("packed_items", []) or [])),
                    "gap": f"{gap:g}",
                    "fill_rate": "--" if np.isnan(fill_rate) else f"{fill_rate * 100:.1f}%",
                    "reason": "；".join(reasons) if reasons else "需要复核",
                })
        return rows

    def fill_failed_table(self) -> None:
        if not hasattr(self, "failed_table"):
            return
        rows = self.extract_failure_rows()
        self.failed_table.setRowCount(max(1, len(rows)))
        headers = ["类型", "托盘", "订单", "状态", "箱号", "箱型/托盘型", "数量", "缺口", "填充率", "原因"]
        if self.failed_table.columnCount() != len(headers):
            self.failed_table.setColumnCount(len(headers))
            self.failed_table.setHorizontalHeaderLabels(headers)
        if not rows:
            vals = ["无", "--", "--", "SUCCESS", "--", "--", "0", "0", "--", "没有发现失败箱或失败托盘。"]
            for c, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(str(val))
                item.setTextAlignment(QtCore.Qt.AlignCenter if c != 9 else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                self.failed_table.setItem(0, c, item)
            return
        for r, row in enumerate(rows):
            vals = [row.get("kind", "--"), row.get("pallet_id", "--"), row.get("order_no", "--"), row.get("status", "--"), row.get("box_id", "--"), row.get("box_type", "--"), row.get("count", "--"), row.get("gap", "--"), row.get("fill_rate", "--"), row.get("reason", "--")]
            is_bad = safe_str(row.get("kind"), "") in {"失败箱", "失败托盘"}
            for c, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(str(val))
                item.setData(QtCore.Qt.UserRole, row.get("pallet_id", "--"))
                item.setTextAlignment(QtCore.Qt.AlignCenter if c != 9 else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if is_bad:
                    item.setBackground(QtGui.QColor("#fee2e2"))
                self.failed_table.setItem(r, c, item)
        self.failed_table.resizeRowsToContents()

    def on_failed_table_double_clicked(self, row: int, column: int) -> None:
        if not hasattr(self, "failed_table"):
            return
        item = self.failed_table.item(row, 1) or self.failed_table.item(row, 0)
        if item is None:
            return
        pallet_id = safe_str(item.data(QtCore.Qt.UserRole), safe_str(item.text(), "--"))
        if pallet_id and pallet_id != "--":
            self.focus_pallet_by_id(pallet_id)

    def on_pallet_changed(self, idx: int):
        if idx < 0 or idx >= len(self.filtered_pallets):
            return
        self.load_pallet(self.filtered_pallets[idx])

    def get_pallet_dims(self, pallet: Dict[str, Any]) -> Tuple[float, float, float]:
        items = pallet.get("packed_items", []) or []
        if items:
            dims = items[0].get("pallet_dims", {}) or {}
            L = safe_float(dims.get("length"), np.nan)
            W = safe_float(dims.get("width"), np.nan)
            H = safe_float(dims.get("height"), np.nan)
            if not any(np.isnan(v) for v in [L, W, H]) and L > 0 and W > 0 and H > 0:
                return L, W, H
        # 兜底：按箱体最大外廓估计
        max_x = max([safe_float(it.get("position", {}).get("x"), 0) + safe_float(it.get("length"), 0) for it in items] or [1440.0])
        max_y = max([safe_float(it.get("position", {}).get("y"), 0) + safe_float(it.get("width"), 0) for it in items] or [2240.0])
        max_z = max([safe_float(it.get("position", {}).get("z"), 0) + safe_float(it.get("height"), 0) for it in items] or [720.0])
        return max_x, max_y, max_z

    def pallet_to_dataframe(self, pallet: Dict[str, Any]) -> pd.DataFrame:
        rows = []
        items = pallet.get("packed_items", []) or []
        for seq, item in enumerate(items, start=1):
            pos = item.get("position", {}) or {}
            dims = item.get("pallet_dims", {}) or {}
            original_sequence = int(item.get("original_packing_sequence") or seq)
            robot_sequence_raw = item.get("robot_packing_sequence")
            try:
                robot_sequence = int(robot_sequence_raw) if robot_sequence_raw is not None else np.nan
            except (TypeError, ValueError):
                robot_sequence = np.nan
            row = {
                "seq": original_sequence,
                "original_packing_sequence": original_sequence,
                "robot_packing_sequence": robot_sequence,
                "box_id": safe_str(item.get("id"), str(seq)),
                "box_type": safe_str(item.get("type"), safe_str(item.get("包装规格代码"), "--")),
                "mass": safe_float(item.get("weight"), 0.0),
                "x": safe_float(pos.get("x"), 0.0),
                "y": safe_float(pos.get("y"), 0.0),
                "z": safe_float(pos.get("z"), 0.0),
                "lx": safe_float(item.get("length"), 0.0),
                "ly": safe_float(item.get("width"), 0.0),
                "lz": safe_float(item.get("height"), 0.0),
                "min_pack_multiple": safe_float(item.get("min_pack_multiple"), np.nan),
                "volume": safe_float(item.get("volume"), np.nan),
                "is_small_box": bool(item.get("is_small_box", False)),
                "support_ratio_json": safe_float(item.get("support_ratio"), np.nan),
                "supported_area_json": safe_float(item.get("supported_area"), np.nan),
                "suction_box_corner": safe_str(item.get("suction_box_corner"), "--"),
                "suction_cup_corner": safe_str(item.get("suction_cup_corner"), "--"),
                "suction_orientation": safe_str(item.get("suction_orientation"), "--"),
                "suction_cup_x_size": safe_float(item.get("suction_cup_x_size"), np.nan),
                "suction_cup_y_size": safe_float(item.get("suction_cup_y_size"), np.nan),
                "suction_rect_x_min": safe_float(item.get("suction_rect_x_min"), np.nan),
                "suction_rect_x_max": safe_float(item.get("suction_rect_x_max"), np.nan),
                "suction_rect_y_min": safe_float(item.get("suction_rect_y_min"), np.nan),
                "suction_rect_y_max": safe_float(item.get("suction_rect_y_max"), np.nan),
                "support_predecessors": list(item.get("support_predecessors") or []),
                "clearance_predecessors": list(item.get("clearance_predecessors") or []),
                "body_clearance_predecessors": list(item.get("body_clearance_predecessors") or []),
                "all_predecessors": list(item.get("all_predecessors") or []),
                "geometric_sequence_feasible": bool(item.get("geometric_sequence_feasible", False)),
                "pallet_L": safe_float(dims.get("length"), np.nan),
                "pallet_W": safe_float(dims.get("width"), np.nan),
                "pallet_H": safe_float(dims.get("height"), np.nan),
            }
            # 当前业务 case 暂不限制承压：保留 max_load 字段，缺失时后续会按超大值处理。
            row["max_load"] = safe_float(item.get("max_load"), np.nan)
            rows.append(row)
        df = pd.DataFrame(rows)
        if len(df):
            df["layer_id"] = pd.factorize(np.round(df["z"].astype(float), 3))[0] + 1
        return df

    # ------------------------- 计算与刷新 -------------------------
    def load_pallet(self, pallet: Dict[str, Any]):
        try:
            self.current_pallet = pallet
            self.df_raw = self.pallet_to_dataframe(pallet)
            self.selected_row_index = None
            self.calculate_current()
        except Exception as e:
            self.show_error(f"读取托盘失败：{e}")

    def recalculate_current(self):
        if self.current_pallet is None:
            self.show_error("请先选择托盘。")
            return
        self.calculate_current(show_message=True)

    def calculate_current(self, show_message: bool = False):
        if self.current_pallet is None or self.df_raw is None:
            return
        if len(self.df_raw) == 0:
            self.clear_current_views()
            return
        missing = [c for c in REQUIRED_BASE_COLUMNS if c not in self.df_raw.columns]
        if missing:
            raise ValueError(f"缺少必需字段：{missing}")

        df = to_numeric(self.df_raw, ["mass", "x", "y", "z", "lx", "ly", "lz", "max_load"])
        df = df.dropna(subset=REQUIRED_BASE_COLUMNS).reset_index(drop=True)
        df = compute_support_and_load(df, z_tol=float(self.sp_z_tol.value()), floor_tol=float(self.sp_z_tol.value()))
        L, W, H = self.get_pallet_dims(self.current_pallet)
        scores = compute_scores(df, L, W, H, ax_g=float(self.sp_ax.value()), ay_g=float(self.sp_ay.value()), friction=float(self.sp_mu.value()))

        df["risk_text"] = [self.build_box_risk_text(df.loc[i], L, W, H) for i in df.index]
        df["risk_level"] = [self.box_risk_level(text) for text in df["risk_text"].tolist()]

        self.df_eval = df
        self.score_result = scores
        self.prepare_animation_order()
        self.fill_pallet_cards()
        self.fill_score_table()
        self.fill_warning_list()
        self.fill_box_table()
        self.refresh_3d_scene()
        if show_message:
            QtWidgets.QMessageBox.information(self, "完成", "当前托盘稳定性已重新计算。")

    def build_box_risk_text(self, row: pd.Series, L: float, W: float, H: float) -> str:
        risks = []
        tol = 1e-6
        if row["x"] < -tol or row["y"] < -tol or row["z"] < -tol or row["x"] + row["lx"] > L + tol or row["y"] + row["ly"] > W + tol or row["z"] + row["lz"] > H + tol:
            risks.append("箱体越界")
        sr = safe_float(row.get("support_ratio"), 0.0)
        if sr < 0.70:
            risks.append("严重支撑不足")
        elif sr < 0.90:
            risks.append("支撑偏低")
        pu = row.get("pressure_utilization", np.nan)
        if not pd.isna(pu) and safe_float(pu) > 1.0:
            risks.append("承压超限")
        if row.get("suction_box_corner") in VALID_CORNER_NAMES and row.get("suction_cup_corner") in VALID_CORNER_NAMES:
            if row.get("suction_box_corner") != row.get("suction_cup_corner"):
                risks.append("吸附角点不一致")
        sx0 = row.get("suction_rect_x_min", np.nan)
        sx1 = row.get("suction_rect_x_max", np.nan)
        sy0 = row.get("suction_rect_y_min", np.nan)
        sy1 = row.get("suction_rect_y_max", np.nan)
        if not any(pd.isna(v) for v in [sx0, sx1, sy0, sy1]):
            if sx0 < -tol or sy0 < -tol or sx1 > L + tol or sy1 > W + tol:
                risks.append("吸盘矩形越界")
        return "；".join(risks) if risks else "正常"

    def box_risk_level(self, text: str) -> int:
        if text == "正常":
            return 0
        if "严重" in text or "超限" in text or "箱体越界" in text:
            return 2
        return 1

    def prepare_animation_order(self):
        if self.df_eval is None:
            self.animation_order = []
            return
        self.animation_order = list(self.df_eval.sort_values(["seq", "z", "y", "x"]).index)
        self.animation_idx = len(self.animation_order)

    def fill_pallet_cards(self):
        if self.current_pallet is None or self.df_eval is None or self.score_result is None:
            return
        df = self.df_eval
        s = self.score_result
        total_score = float(s.get("total_score", 0.0))
        level = score_level(total_score)
        level_state = "good" if total_score >= 70 else ("warn" if total_score >= 60 else "bad")
        mpm_status = safe_str(self.current_pallet.get("mpm_status"), "UNKNOWN")
        mpm_gap = safe_float(self.current_pallet.get("mpm_gap"), 0.0)
        mpm_total = safe_float(self.current_pallet.get("mpm_total"), 0.0)
        mpm_target = safe_float(self.current_pallet.get("mpm_target"), 0.0)
        risk_count = int((df["risk_level"] > 0).sum())
        serious_count = int((df["risk_level"] > 1).sum())
        support_avg = float(df["support_ratio"].mean()) if len(df) else 0.0
        cg_offset = self.score_result.get("cg_offset", (np.nan, np.nan))[1]

        self.card_score.set_data(f"{total_score:.1f}", score_status(total_score), level_state)
        self.card_level.set_data(level, f"风险箱 {risk_count} / 严重 {serious_count}", level_state)
        self.card_boxes.set_data(str(len(df)), f"层数 {s.get('layer_count', '--')}")
        self.card_mass.set_data(f"{s.get('total_mass', 0):.1f} kg", "整托盘总质量")
        self.card_height.set_data(f"{s.get('height_utilization', 0) * 100:.1f}%", "最高箱体 / 托盘限高", "warn" if s.get("height_utilization", 0) > 1 else "normal")
        self.card_support.set_data(f"{support_avg * 100:.1f}%", "按接触面积重算", "good" if support_avg >= 0.95 else ("warn" if support_avg >= 0.90 else "bad"))
        self.card_cg.set_data(f"{cg_offset * 100:.1f}%", "相对半对角线", "good" if cg_offset <= 0.12 else ("warn" if cg_offset <= 0.25 else "bad"))
        self.card_mpm.set_data(mpm_status, f"{mpm_total:g}/{mpm_target:g}, gap={mpm_gap:g}", "good" if mpm_status == "SUCCESS" else "bad")

    def fill_score_table(self):
        self.score_table.setRowCount(0)
        if self.score_result is None:
            return
        s = self.score_result
        rows = []
        mapping = [
            ("重心高度", "cg_height", "质心高度/限高", "越低越稳"),
            ("重心偏移", "cg_offset", "偏移/半对角线", "越靠近托盘中心越稳"),
            ("支撑充分性", "support", "面积加权平均支撑率", "支撑率越高越稳"),
            ("承压安全", "pressure", "最大承压利用率", "当前 case 暂不限制承压，缺 max_load 按超大值处理"),
            ("抗倾覆", "tip", "安全系数K", "加速度扰动下越大越稳"),
            ("抗滑移", "slide", "摩擦/加速度", "越大越不易滑移"),
        ]
        pressure_unlimited = False
        try:
            pressure_unlimited = bool(
                self.df_eval is not None
                and "max_load_unlimited" in self.df_eval.columns
                and self.df_eval["max_load_unlimited"].fillna(False).all()
            )
        except Exception:
            pressure_unlimited = False

        for zh, key, cur_desc, desc in mapping:
            val = s.get(key)
            if val is None:
                if key == "pressure":
                    rows.append([zh, "100.0", "不限制", "优秀", "当前 case 不考虑承压，max_load 缺失时按超大值处理"])
                else:
                    rows.append([zh, "--", "缺数据", "未参与", desc])
            else:
                score, current = val
                if key == "pressure" and pressure_unlimited:
                    rows.append([zh, f"{score:.1f}", "不限制", score_status(float(score)), "当前 case 不考虑承压，max_load 按超大值处理"])
                else:
                    rows.append([zh, f"{score:.1f}", f"{current:.4g}", score_status(float(score)), desc])
        rows.append(["综合评分", f"{float(s.get('total_score', 0)):.1f}", "--", score_level(float(s.get('total_score', 0))), "按可计算指标权重归一化"])
        self.score_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QtWidgets.QTableWidgetItem(str(val))
                item.setTextAlignment(QtCore.Qt.AlignCenter if c != 4 else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if c == 3:
                    text = str(val)
                    if text in ["优秀", "良好", "A", "B"]:
                        item.setBackground(QtGui.QColor("#dcfce7"))
                    elif text in ["一般", "C"]:
                        item.setBackground(QtGui.QColor("#fef3c7"))
                    elif text in ["风险", "D"]:
                        item.setBackground(QtGui.QColor("#fee2e2"))
                self.score_table.setItem(r, c, item)

    def fill_warning_list(self):
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
            return
        for _, row in risk_df.sort_values(["risk_level", "support_ratio"], ascending=[False, True]).head(40).iterrows():
            self.warning_list.addItem(f"第{int(row['seq'])}箱 / {row['box_id']} / {row['box_type']}：{row['risk_text']}")

    def fill_box_table(self):
        self.box_table.setRowCount(0)
        if self.df_eval is None:
            return
        df = self.df_eval
        mode_text = self.cmb_sequence_mode.currentText() if hasattr(self, "cmb_sequence_mode") else "原算法顺序"
        robot_available = (
            mode_text == "机器人执行顺序"
            and str((getattr(self, "current_pallet", None) or {}).get("sequence_status")) == "GEOMETRICALLY_FEASIBLE"
            and "robot_packing_sequence" in df.columns
            and df["robot_packing_sequence"].notna().all()
        )
        sort_column = "robot_packing_sequence" if robot_available else "original_packing_sequence"
        self.box_table.setRowCount(len(df))
        for visual_row, (_, row) in enumerate(df.sort_values([sort_column, "z", "y", "x"]).iterrows()):
            robot_seq = row.get("robot_packing_sequence", np.nan)
            vals = [
                int(row.get("original_packing_sequence", row["seq"])),
                "--" if pd.isna(robot_seq) else int(robot_seq),
                row["box_id"],
                row["box_type"],
                f"{row['lx']:.0f}×{row['ly']:.0f}×{row['lz']:.0f}",
                f"{row['mass']:.3f}",
                f"{row['x']:.1f}",
                f"{row['y']:.1f}",
                f"{row['z']:.1f}",
                int(row.get("layer_id", 0)),
                f"{row['support_ratio'] * 100:.1f}%",
                f"{row['support_area']:.0f}",
                "不限制" if bool(row.get("max_load_unlimited", False)) else ("--" if pd.isna(row.get("pressure_utilization", np.nan)) else f"{row['pressure_utilization'] * 100:.1f}%"),
                row.get("suction_box_corner", "--"),
                row.get("suction_cup_corner", "--"),
                row.get("suction_orientation", "--"),
                self.format_suction_rect(row),
                "--" if pd.isna(row.get("min_pack_multiple", np.nan)) else f"{row['min_pack_multiple']:g}",
                row.get("risk_text", "正常"),
            ]
            idx_item = None
            bg = None
            if int(row.get("risk_level", 0)) >= 2:
                bg = QtGui.QColor("#fee2e2")
            elif int(row.get("risk_level", 0)) == 1:
                bg = QtGui.QColor("#fef3c7")
            for c, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(str(val))
                item.setData(QtCore.Qt.UserRole, int(row.name))
                if c in [0, 1, 5, 6, 7, 8, 9, 10, 11, 12, 17]:
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if bg is not None:
                    item.setBackground(bg)
                self.box_table.setItem(visual_row, c, item)
                if c == 0:
                    idx_item = item
        self.box_table.resizeRowsToContents()

    def format_suction_rect(self, row: pd.Series) -> str:
        vals = [row.get("suction_rect_x_min", np.nan), row.get("suction_rect_x_max", np.nan), row.get("suction_rect_y_min", np.nan), row.get("suction_rect_y_max", np.nan)]
        if any(pd.isna(v) for v in vals):
            return "--"
        return f"x[{vals[0]:.0f},{vals[1]:.0f}] y[{vals[2]:.0f},{vals[3]:.0f}]"

    def on_table_selection_changed(self):
        selected = self.box_table.selectedItems()
        if not selected:
            self.selected_row_index = None
            self.box_detail.setPlainText("在表格中选择一个箱子后显示详细信息。")
            self.refresh_3d_scene()
            return
        row = selected[0].row()
        item = self.box_table.item(row, 0)
        if item is None:
            return
        self.selected_row_index = int(item.data(QtCore.Qt.UserRole))
        self.update_selected_box_detail()
        self.refresh_3d_scene()

    def update_selected_box_detail(self):
        if self.df_eval is None or self.selected_row_index is None or self.selected_row_index not in self.df_eval.index:
            return
        r = self.df_eval.loc[self.selected_row_index]

        pressure_util = r.get("pressure_utilization", np.nan)
        if bool(r.get("max_load_unlimited", False)):
            pressure_util_text = "不限制（max_load按超大值处理）"
        elif pd.isna(pressure_util):
            pressure_util_text = "--"
        else:
            pressure_util_text = f"{float(pressure_util) * 100:.2f}%"
        robot_seq = r.get("robot_packing_sequence", np.nan)
        robot_seq_text = "--" if pd.isna(robot_seq) else str(int(robot_seq))
        text = (
            f"原算法序号：{int(r.get('original_packing_sequence', r['seq']))}\n"
            f"机器人序号：{robot_seq_text}\n"
            f"箱号：{r['box_id']}\n"
            f"类型：{r['box_type']}\n"
            f"尺寸：{r['lx']:.1f} × {r['ly']:.1f} × {r['lz']:.1f} mm\n"
            f"重量：{r['mass']:.4f} kg\n"
            f"位置：X={r['x']:.1f}, Y={r['y']:.1f}, Z={r['z']:.1f} mm\n"
            f"层号：{int(r.get('layer_id', 0))}\n\n"
            f"支撑面积：{r['support_area']:.1f} mm²\n"
            f"支撑率：{r['support_ratio'] * 100:.2f}%\n"
            f"JSON原始支撑率：{fmt_num(r.get('support_ratio_json', np.nan) * 100 if not pd.isna(r.get('support_ratio_json', np.nan)) else np.nan, 2, '%')}\n"
            f"上方载荷：{r['load_above']:.4f} kg\n"
            f"承压利用率：{pressure_util_text}\n\n"
            f"箱子吸附角：{r.get('suction_box_corner', '--')}\n"
            f"吸盘对齐角：{r.get('suction_cup_corner', '--')}\n"
            f"吸盘规格：{r.get('suction_orientation', '--')}\n"
            f"吸盘尺寸：{fmt_num(r.get('suction_cup_x_size', np.nan), 0)} × {fmt_num(r.get('suction_cup_y_size', np.nan), 0)} mm\n"
            f"吸盘矩形：{self.format_suction_rect(r)}\n"
            f"支撑前置箱：{', '.join(map(str, r.get('support_predecessors', []))) or '--'}\n"
            f"净空前置箱：{', '.join(map(str, r.get('clearance_predecessors', []))) or '--'}\n"
            f"全部前置箱：{', '.join(map(str, r.get('all_predecessors', []))) or '--'}\n\n"
            f"风险：{r.get('risk_text', '正常')}"
        )
        self.box_detail.setPlainText(text)

    # ------------------------- 3D -------------------------
    def clear_scene_items(self):
        if not HAS_GL or not hasattr(self, "view3d"):
            return
        for item in self.scene_items + self.mesh_items:
            try:
                self.view3d.removeItem(item)
            except Exception:
                pass
        self.scene_items.clear()
        self.mesh_items.clear()

    def add_line_item(self, pts: List[List[float]], color=(0.1, 0.1, 0.1, 1.0), width: float = 2.0):
        if not HAS_GL:
            return None
        item = gl.GLLinePlotItem(pos=np.array(pts, dtype=float), color=color, width=width, antialias=True, mode="lines")
        self.view3d.addItem(item)
        self.scene_items.append(item)
        return item

    def add_pallet_wireframe(self, L: float, W: float, H: float):
        corners = np.array([
            [0, 0, 0], [L, 0, 0], [L, W, 0], [0, W, 0],
            [0, 0, H], [L, 0, H], [L, W, H], [0, W, H],
        ], dtype=float)
        edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
        pts = []
        for a, b in edges:
            pts.append(corners[a].tolist())
            pts.append(corners[b].tolist())
        self.add_line_item(pts, color=(0.29, 0.16, 0.04, 1.0), width=3.0)

    def add_axes(self, L: float, W: float, H: float):
        length = max(L, W) * 0.28
        z = -25.0
        self.add_line_item([[0, 0, z], [length, 0, z]], color=(0.85, 0.10, 0.10, 1.0), width=2.2)
        self.add_line_item([[0, 0, z], [0, length, z]], color=(0.10, 0.60, 0.20, 1.0), width=2.2)
        self.add_line_item([[0, 0, 0], [0, 0, min(H, length)]], color=(0.10, 0.25, 0.85, 1.0), width=2.2)

    def get_color_values(self) -> Tuple[np.ndarray, str, float, float, bool]:
        if self.df_eval is None or len(self.df_eval) == 0:
            return np.array([]), "数值", 0.0, 1.0, True
        mode = self.cmb_color.currentText()
        df = self.df_eval
        categorical = False
        if mode == "按重量着色":
            vals = df["mass"].to_numpy(dtype=float)
            title = "重量"
        elif mode == "按支撑风险着色":
            vals = (1.0 - df["support_ratio"].fillna(0).to_numpy(dtype=float))
            title = "支撑风险"
        elif mode == "按层高着色":
            vals = df["z"].to_numpy(dtype=float)
            title = "层高"
        elif mode == "按箱型着色":
            vals = np.arange(len(df), dtype=float)
            title = "箱型"
            categorical = True
        else:
            vals = np.arange(len(df), dtype=float)
            title = "吸盘规格"
            categorical = True
        vmin = float(np.nanmin(vals)) if len(vals) else 0.0
        vmax = float(np.nanmax(vals)) if len(vals) else 1.0
        if abs(vmax - vmin) < 1e-12:
            vmax = vmin + 1.0
        return vals, title, vmin, vmax, categorical

    def row_color(self, idx: int, vals: np.ndarray, vmin: float, vmax: float, categorical: bool):
        if self.df_eval is None:
            return (0.3, 0.5, 0.9, 0.86)
        row = self.df_eval.loc[idx]
        mode = self.cmb_color.currentText()
        if categorical:
            if mode == "按箱型着色":
                return categorical_rgba(row.get("box_type", "--"), 0.86)
            return categorical_rgba(row.get("suction_orientation", "--"), 0.86)
        val = float(vals[idx]) if idx < len(vals) else 0.0
        return blue_red_rgba((val - vmin) / (vmax - vmin), alpha=0.86)

    def add_box_mesh(self, row: pd.Series, color):
        item = gl.GLMeshItem(
            meshdata=self.mesh_box_template,
            smooth=False,
            color=color,
            shader="shaded",
            drawEdges=True,
            edgeColor=(0.10, 0.10, 0.10, 0.55),
            glOptions="translucent",
        )
        item.scale(float(row["lx"]), float(row["ly"]), float(row["lz"]))
        item.translate(float(row["x"]), float(row["y"]), float(row["z"]))
        self.view3d.addItem(item)
        self.mesh_items.append(item)

    def add_box_outline(self, row: pd.Series, color=(0.88, 0.10, 0.10, 1.0), width: float = 4.0):
        x0, y0, z0 = float(row["x"]), float(row["y"]), float(row["z"])
        x1, y1, z1 = x0 + float(row["lx"]), y0 + float(row["ly"]), z0 + float(row["lz"])
        pts = [
            [x0, y0, z0], [x1, y0, z0], [x1, y0, z0], [x1, y1, z0], [x1, y1, z0], [x0, y1, z0], [x0, y1, z0], [x0, y0, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y0, z1], [x1, y1, z1], [x1, y1, z1], [x0, y1, z1], [x0, y1, z1], [x0, y0, z1],
            [x0, y0, z0], [x0, y0, z1], [x1, y0, z0], [x1, y0, z1], [x1, y1, z0], [x1, y1, z1], [x0, y1, z0], [x0, y1, z1],
        ]
        self.add_line_item(pts, color=color, width=width)

    def add_suction_rect(self, row: pd.Series):
        vals = [row.get("suction_rect_x_min", np.nan), row.get("suction_rect_x_max", np.nan), row.get("suction_rect_y_min", np.nan), row.get("suction_rect_y_max", np.nan)]
        if any(pd.isna(v) for v in vals):
            return
        x0, x1, y0, y1 = float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3])
        z = float(row["z"] + row["lz"] + 8.0)
        pts = [[x0, y0, z], [x1, y0, z], [x1, y0, z], [x1, y1, z], [x1, y1, z], [x0, y1, z], [x0, y1, z], [x0, y0, z]]
        self.add_line_item(pts, color=(0.96, 0.45, 0.05, 0.95), width=2.2)

    def add_cg_marker(self):
        if self.score_result is None or not HAS_GL:
            return
        x, y, z = self.score_result["cg_x"], self.score_result["cg_y"], self.score_result["cg_z"]
        item = gl.GLScatterPlotItem(pos=np.array([[x, y, z]], dtype=float), color=(0.90, 0.05, 0.05, 1.0), size=18)
        self.view3d.addItem(item)
        self.scene_items.append(item)
        self.add_line_item([[x, y, 0], [x, y, z]], color=(0.90, 0.05, 0.05, 0.95), width=2.0)

    def add_cg_projection_marker(self, L: float, W: float):
        if self.score_result is None or not HAS_GL:
            return
        x, y = self.score_result["cg_x"], self.score_result["cg_y"]
        z = 4.0
        item = gl.GLScatterPlotItem(pos=np.array([[x, y, z]], dtype=float), color=(0.90, 0.05, 0.05, 1.0), size=13)
        self.view3d.addItem(item)
        self.scene_items.append(item)
        r = max(min(L, W) * 0.025, 25.0)
        self.add_line_item([[x - r, y, z], [x + r, y, z], [x, y - r, z], [x, y + r, z]], color=(0.90, 0.05, 0.05, 0.95), width=2.4)
        # 简单安全区：托盘中心附近 60% 矩形，用于直观看重心是否偏移过大。
        sx0, sx1 = L * 0.20, L * 0.80
        sy0, sy1 = W * 0.20, W * 0.80
        pts = [[sx0, sy0, z], [sx1, sy0, z], [sx1, sy0, z], [sx1, sy1, z], [sx1, sy1, z], [sx0, sy1, z], [sx0, sy1, z], [sx0, sy0, z]]
        self.add_line_item(pts, color=(0.15, 0.45, 0.90, 0.65), width=1.8)

    def set_view_preset(self, mode: str) -> None:
        if not HAS_GL or not hasattr(self, "view3d"):
            return
        L, W, H = self.get_pallet_dims(self.current_pallet or {})
        distance = max(L, W, H, 1000.0) * 1.65
        presets = {
            "top": dict(elevation=89.0, azimuth=-90.0, distance=distance),
            "front": dict(elevation=8.0, azimuth=-90.0, distance=distance),
            "side": dict(elevation=8.0, azimuth=0.0, distance=distance),
            "iso": dict(elevation=25.0, azimuth=-55.0, distance=distance),
        }
        cfg = presets.get(mode, presets["iso"])
        try:
            self.view3d.setCameraPosition(distance=cfg["distance"], elevation=cfg["elevation"], azimuth=cfg["azimuth"])
        except Exception:
            self.view3d.opts.update(cfg)
        self.view3d.update()

    def show_final_result(self):
        if self.df_eval is None:
            return
        self.anim_timer.stop()
        if not self.animation_order:
            self.prepare_animation_order()
        self.animation_idx = len(self.animation_order)
        self.refresh_3d_scene()
        if hasattr(self, "workspace_tabs"):
            self.workspace_tabs.setCurrentIndex(0)

    def refresh_3d_scene(self, *args):
        if not HAS_GL or self.df_eval is None:
            return
        self.clear_scene_items()
        L, W, H = self.get_pallet_dims(self.current_pallet or {})
        grid = gl.GLGridItem()
        grid.setSize(x=max(L, W) * 1.8, y=max(L, W) * 1.8, z=1)
        grid.setSpacing(x=200, y=200, z=1)
        grid.translate(L / 2, W / 2, -5)
        self.view3d.addItem(grid)
        self.scene_items.append(grid)
        self.add_axes(L, W, H)
        self.add_pallet_wireframe(L, W, H)

        vals, title, vmin, vmax, categorical = self.get_color_values()
        self.colorbar.set_info(title, vmin, vmax)

        indices = list(self.animation_order)
        if self.animation_idx < len(indices):
            indices = indices[:max(0, self.animation_idx)]
        only_risk = self.chk_only_risk.isChecked()
        risk_outline = (not hasattr(self, "chk_risk_outline")) or self.chk_risk_outline.isChecked()
        for idx in indices:
            row = self.df_eval.loc[idx]
            risk_level = int(row.get("risk_level", 0))
            if only_risk and risk_level <= 0:
                continue
            self.add_box_mesh(row, self.row_color(idx, vals, vmin, vmax, categorical))
            if risk_outline and risk_level > 0:
                if risk_level >= 2:
                    self.add_box_outline(row, color=(0.90, 0.05, 0.05, 1.0), width=4.0)
                else:
                    self.add_box_outline(row, color=(0.95, 0.55, 0.05, 1.0), width=3.0)
            if self.chk_suction.isChecked():
                self.add_suction_rect(row)

        if self.selected_row_index is not None and self.selected_row_index in self.df_eval.index:
            self.add_box_outline(self.df_eval.loc[self.selected_row_index], color=(0.10, 0.10, 0.10, 1.0), width=5.5)

        self.add_cg_marker()
        if (not hasattr(self, "chk_cg_projection")) or self.chk_cg_projection.isChecked():
            self.add_cg_projection_marker(L, W)
        self.anim_label.setText(f"进度：{min(self.animation_idx, len(self.animation_order))} / {len(self.animation_order)}")

    def play_animation(self):
        if self.df_eval is None:
            return
        if not self.animation_order:
            self.prepare_animation_order()
        if self.animation_idx >= len(self.animation_order):
            self.animation_idx = 0
        self.anim_timer.start(550)

    def pause_animation(self):
        self.anim_timer.stop()

    def reset_animation(self):
        self.anim_timer.stop()
        self.animation_idx = 0
        self.refresh_3d_scene()

    def _anim_step(self):
        if self.df_eval is None:
            return
        self.animation_idx += 1
        if self.animation_idx > len(self.animation_order):
            self.anim_timer.stop()
            self.animation_idx = len(self.animation_order)
        self.refresh_3d_scene()

    # ------------------------- 其它 -------------------------
    def clear_current_views(self):
        self.box_table.setRowCount(0)
        self.score_table.setRowCount(0)
        self.warning_list.clear()
        self.box_detail.setPlainText("暂无托盘数据。")
        self.clear_scene_items()
        for card in [self.card_score, self.card_level, self.card_boxes, self.card_mass, self.card_height, self.card_support, self.card_cg, self.card_mpm]:
            card.set_data("--", "")

    def export_eval_table(self):
        if self.df_eval is None or self.current_pallet is None:
            self.show_error("请先加载并选择一个托盘。")
            return
        default_name = f"{safe_str(self.current_pallet.get('pallet_id'), 'pallet')}_stability_eval.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出当前托盘评价表",
            default_name,
            "CSV Files (*.csv);;Excel Files (*.xlsx)",
        )
        if not path:
            return
        try:
            out = self.df_eval.copy()
            out["pallet_id"] = safe_str(self.current_pallet.get("pallet_id"))
            out["mpm_status"] = safe_str(self.current_pallet.get("mpm_status"))
            out["mpm_gap"] = safe_float(self.current_pallet.get("mpm_gap"), 0.0)
            if path.lower().endswith(".xlsx"):
                out.to_excel(path, index=False)
            else:
                out.to_csv(path, index=False, encoding="utf-8-sig")
            QtWidgets.QMessageBox.information(self, "导出完成", f"已导出：\n{path}")
        except Exception as e:
            self.show_error(f"导出失败：{e}")

    def show_error(self, msg: str):
        QtWidgets.QMessageBox.critical(self, "错误", str(msg))


# ============================================================
# 程序入口
# ============================================================

def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()

    # 支持命令行直接传入 JSON 路径：python stability_business_dashboard_json.py xxx.json
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if path.exists():
            QtCore.QTimer.singleShot(100, lambda: win.load_json_file(path))
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
