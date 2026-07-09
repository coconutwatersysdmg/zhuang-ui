"""
新装箱入口

完全使用 src/ 内模块装配 PackingWorkflow，不依赖 zhuangxiang.py。

用法:
    python run_packing.py
    python run_packing.py --out plan.json                    # 另存方案 JSON
    python run_packing.py --max-boxes 800 --out subset.json  # 子集快速通道
    python run_packing.py --profile                          # cProfile 热点定位
    python run_packing.py --safe                             # 配方/基线双跑审计
"""

import json
import sys
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
from src.data import load_boxes
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


def _parse_cli(argv):
    out_path = None
    max_boxes = None
    profile = False
    safe_compare = False
    config_path = None
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
        else:
            i += 1
    return out_path, max_boxes, profile, safe_compare, config_path


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
    out_path, max_boxes, profile, safe_compare, config_path = _parse_cli(
        sys.argv[1:]
    )
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
