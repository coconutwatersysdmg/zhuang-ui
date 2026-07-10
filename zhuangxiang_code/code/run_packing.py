"""
新装箱入口

完全使用 src/ 内模块装配 PackingWorkflow，不依赖 zhuangxiang.py。

用法:
    python run_packing.py
    python run_packing.py --out plan.json                    # 另存方案 JSON
    python run_packing.py --max-boxes 800 --out subset.json  # 子集快速通道
    python run_packing.py --profile                          # cProfile 热点定位
    python run_packing.py --safe                             # 配方/基线双跑审计
    python run_packing.py --api --config cfg.yaml            # 接口模式（常驻，每 200s 拉取）
"""

import json
import shutil
import sys
import threading
import time
from functools import partial
from pathlib import Path

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

import pandas as pd

from src.config import (
    DATA_DIR,
    ENABLE_EXPENSIVE_FAILED_REPACK,
    OUTPUT_DIR,
    PALLET_INDEX_TARGETS,
    ConstraintConfig,
    ConfigLoader,
)
from src.data import (
    configure_reference_excel,
    fetch_and_save_stock_json,
    load_boxes,
    load_boxes_from_local_json,
)
from src.data.api_loader import stock_api_url
from src.geometry import validate_center_of_mass
from src.main import PackingWorkflow, build_json_output_plan
from src.main.report_persister import JsonFileReportPersister
from src.packing import (
    BeamSearchPacker,
    build_centered_single_box_solution,
    build_direct_layer_packing_solution,
)
from src.rescue import (
    FailedPoolRebuilder,
    LowFillRepacker,
    LowLoadRebuilder,
    RescueOptimizer,
    TailFragmentAbsorber,
    fast_rescue_failed_pallets_by_hole_fill,
    fast_rescue_failed_pallets_by_topup,
    rescue_by_recipe_rebuild,
)


class _DynamicRescueOptimizer:
    """为每个分组按其 pallet_dims 懒构造 RescueOptimizer。"""

    def __init__(self, enable_expensive_repack: bool):
        self._enable = enable_expensive_repack
        self._cache: dict = {}

    def optimize_failed_by_failed(self, type_plans, target_mpm):
        pallet_dims = {}
        for plan in type_plans:
            for item in plan.get('packed_items', []):
                pd_info = item.get('pallet_dims')
                if pd_info:
                    pallet_dims = pd_info
                    break
            if pallet_dims:
                break
        key = (
            pallet_dims.get('length', 0),
            pallet_dims.get('width', 0),
            pallet_dims.get('height', 0),
        )
        if key not in self._cache:
            self._cache[key] = RescueOptimizer(
                pallet_dims=pallet_dims,
                enable_expensive_repack=self._enable,
            )
        return self._cache[key].optimize_failed_by_failed(type_plans, target_mpm)


def load_constraint_config(config_path=None) -> ConstraintConfig:
    """加载约束统一配置。

    优先级：显式 --config 路径 > 默认 config/packing_config.yaml > 内置默认值。
    任何加载失败都安全回退到内置默认（与历史行为一致），不阻断装箱。
    """
    if config_path is None:
        default_yaml = project_root / 'config' / 'packing_config.yaml'
        config_path = default_yaml if default_yaml.exists() else None
    if config_path is None:
        return ConstraintConfig()
    if not Path(config_path).exists():
        print(f'警告：配置文件 {config_path} 不存在，改用内置默认约束配置。')
        return ConstraintConfig()
    try:
        return ConfigLoader(Path(config_path)).load_constraint_config()
    except (OSError, ValueError, KeyError) as exc:
        print(f'警告：约束配置 {config_path} 加载失败（{exc}），改用内置默认。')
        return ConstraintConfig()


def load_data_filepath(config_path=None):
    """从配置读取数据集路径（excel_data.source_file，相对 DATA_DIR）。

    优先级：显式 --config 的 excel_data.source_file > 默认 packing_config.yaml
    的同字段 > None（None 时由 load_boxes 回退内置 SMALL_BOX_SOURCE_FILE）。
    文件不存在或加载失败都返回 None，安全回退默认数据集，不阻断装箱。
    """
    if config_path is None:
        default_yaml = project_root / 'config' / 'packing_config.yaml'
        config_path = default_yaml if default_yaml.exists() else None
    if config_path is None:
        return None
    if not Path(config_path).exists():
        print(f'警告：配置文件 {config_path} 不存在，改用内置默认数据集。')
        return None
    try:
        excel_cfg = ConfigLoader(Path(config_path)).load_excel_config()
    except (OSError, ValueError, KeyError) as exc:
        print(f'警告：数据集配置 {config_path} 加载失败（{exc}），改用内置默认数据集。')
        return None
    source_file = getattr(excel_cfg, 'source_file', None)
    if not source_file:
        return None
    full = DATA_DIR / source_file
    if not full.exists():
        print(f'警告：配置数据集 {full} 不存在，改用内置默认数据集。')
        return None
    return str(full)


def load_data_source_config(config_path=None):
    """读取数据源配置。返回 dict，含 mode/api_base_url/download_interval 等。"""
    defaults = {
        "mode": "manual",
        "api_base_url": "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
        "download_interval": 200,
        "input_dir": "input",
        "bms_reference_file": "668箱子数据集.xlsx",
    }
    if config_path is None:
        default_yaml = project_root / "config" / "packing_config.yaml"
        config_path = default_yaml if default_yaml.exists() else None
    if config_path is None or not Path(config_path).exists():
        return defaults
    try:
        data = ConfigLoader(Path(config_path)).config_data or {}
    except (OSError, ValueError, KeyError):
        return defaults
    src = data.get("data_source") or {}
    merged = dict(defaults)
    merged.update({k: v for k, v in src.items() if v is not None})
    merged["mode"] = str(merged.get("mode", "manual")).strip().lower()
    merged["download_interval"] = int(merged.get("download_interval", 200) or 200)
    return merged


def load_run_config(config_path=None):
    """读取运行模式配置。返回 (run_mode, incremental_filepath)。

    run_mode: 'normal'(默认) | 'incremental'。
    incremental_filepath: 增量三表 Excel 完整路径(相对 data/)，文件不存在返回 None。
    """
    if config_path is None:
        default_yaml = project_root / 'config' / 'packing_config.yaml'
        config_path = default_yaml if default_yaml.exists() else None
    if config_path is None or not Path(config_path).exists():
        return 'normal', None
    try:
        data = ConfigLoader(Path(config_path)).config_data or {}
    except (OSError, ValueError, KeyError):
        return 'normal', None
    run_mode = str(data.get('run_mode', 'normal')).strip().lower()
    incr_file = None
    src = (data.get('incremental') or {}).get('source_file')
    if src:
        full = DATA_DIR / src
        incr_file = str(full) if full.exists() else None
        if src and incr_file is None:
            print(f'警告：增量数据文件 {full} 不存在。')
    return run_mode, incr_file


def build_workflow(
    safe_compare: bool = False,
    constraint_config: ConstraintConfig = None,
) -> PackingWorkflow:
    """组装 PackingWorkflow。所有原语来自 src/。

    Args:
        safe_compare: True 时启用配方/基线双跑审计模式（CLI --safe）。
        constraint_config: 约束统一配置；None 时用内置默认（行为不变）。
            该配置注入主装箱、四个救援器类与三个救援函数，保证放置层与
            门禁层同源。
    """
    if constraint_config is None:
        constraint_config = ConstraintConfig()
    cfg = constraint_config

    return PackingWorkflow(
        preprocess_fn=load_boxes,
        custom_packer_cls=BeamSearchPacker,
        build_direct_layer_solution=build_direct_layer_packing_solution,
        build_centered_single_box_solution=build_centered_single_box_solution,
        validate_center_of_mass=validate_center_of_mass,
        fast_rescue_hole_fill=partial(
            fast_rescue_failed_pallets_by_hole_fill, constraint_config=cfg
        ),
        fast_rescue_topup=partial(
            fast_rescue_failed_pallets_by_topup, constraint_config=cfg
        ),
        rescue_by_recipe_rebuild=partial(
            rescue_by_recipe_rebuild, constraint_config=cfg
        ),
        rescue_optimizer=_DynamicRescueOptimizer(
            enable_expensive_repack=ENABLE_EXPENSIVE_FAILED_REPACK
        ),
        failed_pool_rebuilder=FailedPoolRebuilder(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
            constraint_config=cfg,
        ),
        low_fill_repacker=LowFillRepacker(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
            constraint_config=cfg,
        ),
        tail_fragment_absorber=TailFragmentAbsorber(constraint_config=cfg),
        low_load_rebuilder=LowLoadRebuilder(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
            constraint_config=cfg,
        ),
        make_json_output_plan=partial(
            build_json_output_plan,
            center_of_mass_tolerance=cfg.center_of_mass_tolerance,
        ),
        pallet_index_targets=PALLET_INDEX_TARGETS,
        report_persister=JsonFileReportPersister(
            OUTPUT_DIR,
            lambda fmt: pd.Timestamp.now().strftime(fmt),
        ),
        safe_compare=safe_compare,
        constraint_config=cfg,
    )


class _UiResultReportPersister(JsonFileReportPersister):
    """接口模式下持久化结果，并打印 UI 可识别的结果路径标记。"""

    def persist(self, report, total_runtime: float) -> None:
        super().persist(report, total_runtime)
        candidates = list(self._output_dir.glob("packing_plan_*.json"))
        if candidates:
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            print(f"[UI-RESULT] {latest.resolve()}")


def _parse_cli(argv):
    out_path = None
    max_boxes = None
    profile = False
    safe_compare = False
    config_path = None
    use_api = False
    use_excel = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--out':
            if i + 1 >= len(argv):
                raise SystemExit('错误：--out 缺少路径取值')
            out_path = argv[i + 1]
            i += 2
        elif a == '--max-boxes':
            if i + 1 >= len(argv):
                raise SystemExit('错误：--max-boxes 缺少整数取值')
            max_boxes = int(argv[i + 1])
            i += 2
        elif a == '--config':
            if i + 1 >= len(argv):
                raise SystemExit('错误：--config 缺少路径取值')
            config_path = argv[i + 1]
            i += 2
        elif a == '--profile':
            profile = True
            i += 1
        elif a == '--safe':
            safe_compare = True
            i += 1
        elif a == '--api':
            use_api = True
            i += 1
        elif a == '--excel':
            use_excel = True
            i += 1
        else:
            i += 1
    return out_path, max_boxes, profile, safe_compare, config_path, use_api, use_excel


def _run_incremental(constraint_config, safe_compare, incr_filepath, out_path):
    """增量装箱：先装初始单，再把未达标盘的箱+新增箱重排合并。
    复用 build_workflow（受 main_packer/约束配置控制），与普通模式同一装箱核心。
    """
    from src.incremental import load_incremental_excel, run_incremental_packing
    from src.main.report_persister import NullReportPersister

    def factory():
        wf = build_workflow(safe_compare=safe_compare, constraint_config=constraint_config)
        wf._report_persister = NullReportPersister()
        return wf

    print(f'已启用增量模式，数据文件：{incr_filepath}')
    batch = load_incremental_excel(Path(incr_filepath))
    result = run_incremental_packing(batch.initial_boxes, batch.new_boxes, factory)
    report = result.report
    ov = report['summary']['overall']
    print(f"增量装箱完成：初始 {len(batch.initial_boxes)} 箱 + 新增 {len(batch.new_boxes)} 箱"
          f" → 总托盘 {ov['total_pallets']}，达标 {ov['success_pallets']}，"
          f"未达标 {ov['failed_pallets']}，用时 {result.total_runtime_seconds}s。")
    if out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False)
        print('方案另存为:', out_path)
    return report


def _resolve_input_dir(data_source_cfg: dict) -> Path:
    rel = str(data_source_cfg.get("input_dir", "input") or "input")
    return (project_root / rel).resolve()


def _resolve_bms_reference(data_source_cfg: dict) -> Path:
    rel = str(
        data_source_cfg.get("bms_reference_file", "668箱子数据集.xlsx")
        or "668箱子数据集.xlsx"
    )
    return (DATA_DIR / rel).resolve()


def _download_worker(stop_event: threading.Event, input_dir: Path, base_url: str, interval: int):
    print(f"[下载线程] 启动，每 {interval} 秒请求一次接口...")
    while not stop_event.is_set():
        fetch_and_save_stock_json(input_dir, base_url=base_url)
        for _ in range(max(1, interval)):
            if stop_event.is_set():
                break
            time.sleep(1)
    print("[下载线程] 已停止。")


def _get_pending_input_files(input_dir: Path) -> list:
    if not input_dir.exists():
        return []
    return sorted(input_dir.glob("*.json"))


def _process_worker(
    stop_event: threading.Event,
    workflow: PackingWorkflow,
    input_dir: Path,
    processed_dir: Path,
    bad_dir: Path,
):
    processed_dir.mkdir(parents=True, exist_ok=True)
    bad_dir.mkdir(parents=True, exist_ok=True)
    print("[处理线程] 启动，等待 input/ 目录中出现 JSON 文件...")
    while not stop_event.is_set():
        pending = _get_pending_input_files(input_dir)
        if not pending:
            for _ in range(5):
                if stop_event.is_set():
                    break
                time.sleep(1)
            continue

        filepath = pending[0]
        print(f"\n{'=' * 60}")
        print(f"[处理] 开始处理: {filepath.name}")
        print(f"{'=' * 60}")
        try:
            boxes = load_boxes_from_local_json(str(filepath))
            if not boxes:
                print(f"[处理] 文件 {filepath.name} 数据为空或异常，移至 bad/")
                shutil.move(str(filepath), str(bad_dir / filepath.name))
                continue

            report = workflow.run_with_boxes(boxes)
            if report is None:
                print(f"[处理] 装箱失败: {filepath.name}")

            shutil.move(str(filepath), str(processed_dir / filepath.name))
            print(f"[处理] 完成: {filepath.name} → processed/")
        except Exception as exc:
            print(f"[处理] 异常: {filepath.name} → {exc}")
            try:
                shutil.move(str(filepath), str(bad_dir / filepath.name))
            except Exception:
                pass

    print("[处理线程] 已停止。")


def _run_api_mode(safe_compare: bool = False, config_path=None):
    data_source_cfg = load_data_source_config(config_path)
    constraint_config = load_constraint_config(config_path)
    input_dir = _resolve_input_dir(data_source_cfg)
    processed_dir = input_dir / "processed"
    bad_dir = input_dir / "bad"
    base_url = data_source_cfg.get("api_base_url")
    interval = int(data_source_cfg.get("download_interval", 200) or 200)

    bms_ref = _resolve_bms_reference(data_source_cfg)
    configure_reference_excel(bms_ref)

    workflow = build_workflow(
        safe_compare=safe_compare,
        constraint_config=constraint_config,
    )
    workflow._report_persister = _UiResultReportPersister(
        OUTPUT_DIR,
        lambda fmt: pd.Timestamp.now().strftime(fmt),
    )

    print("=" * 60)
    print("装箱服务（接口模式，持续运行）")
    print(f"  下载间隔: {interval} 秒")
    print(f"  库存接口: POST {stock_api_url(base_url)}")
    print(f"  输入目录: {input_dir}")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  BMS 参考: {bms_ref}")
    print("  按 Ctrl+C 或由 UI 停止按钮结束进程")
    print("=" * 60)

    stop_event = threading.Event()
    downloader = threading.Thread(
        target=_download_worker,
        args=(stop_event, input_dir, base_url, interval),
        daemon=True,
        name="downloader",
    )
    processor = threading.Thread(
        target=_process_worker,
        args=(stop_event, workflow, input_dir, processed_dir, bad_dir),
        daemon=True,
        name="processor",
    )
    downloader.start()
    processor.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        downloader.join(timeout=10)
        processor.join(timeout=10)


def _run(out_path, max_boxes, safe_compare=False, config_path=None):
    constraint_config = load_constraint_config(config_path)
    data_filepath = load_data_filepath(config_path)
    run_mode, incr_filepath = load_run_config(config_path)
    if config_path:
        print(f'已加载约束配置：{config_path}')

    # 增量模式：走两阶段增量编排（用配置的算法+约束）。
    if run_mode == 'incremental':
        if not incr_filepath:
            raise SystemExit('错误：run_mode=incremental 但 incremental.source_file 缺失或文件不存在。')
        return _run_incremental(constraint_config, safe_compare, incr_filepath, out_path)

    workflow = build_workflow(
        safe_compare=safe_compare, constraint_config=constraint_config
    )
    if data_filepath:
        print(f'已加载配置数据集：{data_filepath}')
    if safe_compare:
        print('已启用 --safe 审计模式：配方与基线双跑对比。')
    if max_boxes is None:
        report = workflow.run(data_filepath)
    else:
        # 子集快速通道：截取前 max_boxes 个标准化箱子后直接装箱
        from src.config import SMALL_BOX_SOURCE_FILE
        src_file = data_filepath or str(SMALL_BOX_SOURCE_FILE)
        boxes = load_boxes(src_file)
        report = workflow.run_with_boxes(boxes[:max_boxes])
    if report is not None and out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False)
        print('方案另存为:', out_path)
    return report


if __name__ == '__main__':
    (
        out_path,
        max_boxes,
        profile,
        safe_compare,
        config_path,
        use_api,
        use_excel,
    ) = _parse_cli(sys.argv[1:])

    data_source_cfg = load_data_source_config(config_path)
    api_mode = use_api or (
        not use_excel and data_source_cfg.get("mode") == "api"
    )

    if api_mode:
        _run_api_mode(safe_compare=safe_compare, config_path=config_path)
        sys.exit(0)

    if profile:
        import cProfile
        import pstats
        pr = cProfile.Profile()
        pr.enable()
        report = _run(out_path, max_boxes, safe_compare, config_path)
        pr.disable()
        stats = pstats.Stats(pr).sort_stats('cumulative')
        stats.print_stats(30)
    else:
        report = _run(out_path, max_boxes, safe_compare, config_path)
    if report is None:
        sys.exit(1)
