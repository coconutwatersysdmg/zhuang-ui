"""
装箱工作流编排器

整合 OrderProcessor、PalletPacker、救援链与 ResultFormatter，
驱动端到端装箱流程。
"""

import inspect
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..utils.case_group import CASE_GROUP_ORDER_TAG, split_case_group_tag
from .order_processor import OrderProcessor
from .pallet_packer import PalletPacker
from .recipe_first import pack_group_recipe_first
from .result_formatter import ResultFormatter

# 组内子聚类：混合订单拆出的「杂箱子组」临时订单后缀，输出前 _restore_split_orders 还原。
_SPLIT_REST_TAG = '__SPLITREST__'


class PackingWorkflow:
    """端到端装箱工作流。"""

    def __init__(
        self,
        preprocess_fn: Callable,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        build_centered_single_box_solution: Callable,
        validate_center_of_mass: Callable,
        fast_rescue_hole_fill: Callable,
        fast_rescue_topup: Callable,
        rescue_by_recipe_rebuild: Callable,
        rescue_optimizer,
        failed_pool_rebuilder,
        low_fill_repacker,
        tail_fragment_absorber,
        low_load_rebuilder,
        make_json_output_plan: Callable,
        pallet_index_targets: Dict[str, float],
        report_persister=None,
        safe_compare: bool = False,
        constraint_config=None,
    ):
        if constraint_config is None:
            from ..config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self._constraint_config = constraint_config
        self.order_processor = OrderProcessor(preprocess_fn)
        self.packer = PalletPacker(
            custom_packer_cls,
            build_direct_layer_solution,
            build_centered_single_box_solution,
            validate_center_of_mass,
            constraint_config=constraint_config,
        )
        self.safe_compare = safe_compare
        self._hole_fill = fast_rescue_hole_fill
        self._topup = fast_rescue_topup
        self._recipe_rebuild = rescue_by_recipe_rebuild
        self._rescue_optimizer = rescue_optimizer
        self._failed_pool_rebuilder = failed_pool_rebuilder
        self._low_fill_repacker = low_fill_repacker
        self._tail_fragment_absorber = tail_fragment_absorber
        self._low_load_rebuilder = low_load_rebuilder
        self._make_json_plan = make_json_output_plan
        self._targets = pallet_index_targets
        self._report_persister = report_persister
        # 主装箱算法选择：'gcp' = 全局列式装箱 + 柱级组合优化（默认）；
        # 其它（'beam'）= 旧 beam + 配方优先 + 救援链。
        self._main_packer = getattr(constraint_config, 'main_packer', 'gcp')
        self._gcp_packer = None
        if self._main_packer == 'gcp':
            from ..packing.global_column_packer import GlobalColumnPacker
            self._gcp_packer = GlobalColumnPacker(constraint_config=constraint_config)

    def run(self, data_filepath: Optional[str] = None) -> Optional[Dict]:
        start = time.time()
        all_boxes, grouped = self.order_processor.prepare(data_filepath)
        return self._run_with_prepared(all_boxes, grouped, start)

    def run_with_boxes(self, boxes: List[Dict]) -> Optional[Dict]:
        start = time.time()
        grouped = self.order_processor.group_by_order(boxes or [])
        return self._run_with_prepared(boxes or [], grouped, start)

    def _run_with_prepared(
        self,
        all_boxes: List[Dict],
        grouped: Dict,
        start: float,
    ) -> Optional[Dict]:
        if not all_boxes:
            return None
        print("数据预处理和箱子分组完成（按托盘类型+销售订单号）。\n" + "-" * 40)

        grouped = self._partition_groups(grouped)

        final_plan: List[Dict] = []
        runtime_stats = {
            "group_pack_seconds": 0.0,
            "group_topup_seconds": 0.0,
            "group_retry_seconds": 0.0,
            "group_repack_seconds": 0.0,
            "group_total_seconds": 0.0,
        }
        by_type_stats: Dict[str, Dict] = {}

        for (pallet_type, sales_order_no), boxes_in_group in grouped.items():
            group_start = time.time()
            print(f"正在处理托盘类型：{pallet_type}，销售订单号：{sales_order_no}")
            target_mpm = self._targets.get(pallet_type)
            if target_mpm is None:
                print(f"  - 警告：托盘类型 {pallet_type} 未配置指数目标，将退化为 mpm 总量优先。")

            # 主算法 = 全局列式装箱 + 柱级组合优化（仅对"适合的规则分组"）：
            # 自带凑柱→ILP→网格→残料兜底，不依赖救援链。不适合的复杂/不规则
            # 分组（柱化注定多非满柱）自动回退旧 beam + 配方优先 + 救援链，避免退步。
            if self._gcp_packer is not None and self._gcp_packer.suits_group(
                boxes_in_group, target_mpm
            ):
                print("  - 主算法：全局列式装箱 + 柱级组合优化（GCP）。")
                if self._run_group_gcp(
                    pallet_type, sales_order_no, boxes_in_group, target_mpm,
                    group_start, final_plan, by_type_stats, runtime_stats,
                ):
                    continue
                print("  - GCP 盘数远超理论（CP-SAT 装不满）→ 回退 baseline beam + 配方优先。")
            elif self._gcp_packer is not None:
                print("  - 该分组不适合列式（多非满柱）→ 回退 beam + 配方优先。")

            type_plan, pack_runtime, index_diag = pack_group_recipe_first(
                self.packer, pallet_type, sales_order_no, boxes_in_group,
                target_mpm, safe_compare=self.safe_compare,
                allow_box_rotation=getattr(
                    self._constraint_config, 'allow_box_rotation_90', True),
            )
            self._print_diagnostics(index_diag, target_mpm)

            pallet_dims = boxes_in_group[0]['pallet_dims']
            repack_start = time.time()
            canonical = (index_diag.get("canonical_layer_best") or {}).get("best_mpm")
            geometric_unreachable = (
                target_mpm is not None
                and canonical is not None
                and float(canonical) + 1e-9 < float(target_mpm)
            )
            if geometric_unreachable:
                print(f"  - 几何不可达标记：典型整层上限 {float(canonical):g} < 目标 {float(target_mpm):g}，救援优先压缩托盘数和填充率。")

            main_tail_diag = index_diag.get("main_tail_absorb") or {}
            skip_index_rescue = geometric_unreachable or main_tail_diag.get("tail_absorb_success", 0) > 0
            rescue_timing = {}

            t_stage = time.time()
            pool_diag = {"rescued": 0, "rebuild_attempts": 0, "skipped": True} if geometric_unreachable else self._failed_pool_rebuilder.rebuild(type_plan, pallet_dims, target_mpm)
            rescue_timing["failed_pool_seconds"] = time.time() - t_stage

            t_stage = time.time()
            hf = {"rescued": 0, "hole_fill_tried": 0, "hole_fill_pack_fail": 0, "skipped": True} if skip_index_rescue else self._hole_fill(type_plan, pallet_dims, target_mpm, max_gap=64.0, max_attempts=80, max_donor_scan=160, max_add_items=8)
            rescue_timing["hole_fill_seconds"] = time.time() - t_stage

            t_stage = time.time()
            tu = {"rescued": 0, "topup_tried": 0, "topup_pack_fail": 0, "topup_rejected_missing_receiver": 0, "skipped": True} if skip_index_rescue else self._topup(type_plan, pallet_dims, target_mpm, max_gap=64.0, max_attempts=80, max_donor_scan=80)
            rescue_timing["topup_rescue_seconds"] = time.time() - t_stage

            t_stage = time.time()
            rb = {"rescued": 0, "recipe_rebuild_tried": 0, "recipe_rebuild_success": 0, "skipped": True} if geometric_unreachable else self._recipe_rebuild(type_plan, pallet_dims, target_mpm, max_group_boxes=400, max_recipe_count=12)
            rescue_timing["recipe_rebuild_seconds"] = time.time() - t_stage

            t_stage = time.time()
            low_fill_diag = {"low_fill_tried": 0, "low_fill_accepted": 0, "reason": "geometric_target_unreachable"} if geometric_unreachable else self._low_fill_repacker.repack(type_plan, pallet_dims, target_mpm, geometric_unreachable)
            rescue_timing["low_fill_seconds"] = time.time() - t_stage

            t_stage = time.time()
            tail_diag = {"tail_absorb_tried": 0, "tail_absorb_success": 0, "skipped": True} if main_tail_diag.get("tail_absorb_success", 0) else self._tail_fragment_absorber.absorb(type_plan, pallet_dims, target_mpm)
            rescue_timing["tail_absorb_seconds"] = time.time() - t_stage

            t_stage = time.time()
            low_diag = self._low_load_rebuilder.compact_low_fill_tails(type_plan, pallet_dims, target_mpm) if geometric_unreachable else self._low_load_rebuilder.rebuild(type_plan, pallet_dims, target_mpm)
            rescue_timing["low_load_seconds"] = time.time() - t_stage
            if hasattr(self._low_load_rebuilder, "merge_low_load_pairs"):
                t_stage = time.time()
                low_pair_diag = self._low_load_rebuilder.merge_low_load_pairs(type_plan, pallet_dims, target_mpm)
                rescue_timing["low_pair_seconds"] = time.time() - t_stage
                if low_pair_diag:
                    low_diag.update(low_pair_diag)

            # 互借修复（失败盘合并装满）放在救援链末尾：指数救援全部尝试过后，
            # 把剩余失败盘的箱子合并重装为更少、更满的托盘（棘轮验收，不退步）。
            t_stage = time.time()
            repack = self._call_rescue_optimizer(type_plan, target_mpm, pallet_dims)
            rescue_timing["pair_repack_seconds"] = time.time() - t_stage

            self._drop_empty_pallets(type_plan)
            repack_time = time.time() - repack_start
            rescued = hf.get("rescued", 0) + tu.get("rescued", 0) + rb.get("rescued", 0) + repack.get("rescued", 0) + max(0, low_fill_diag.get("low_fill_new_success", 0) - low_fill_diag.get("low_fill_old_success", 0)) + pool_diag.get("rescued", 0) + max(0, tail_diag.get("tail_absorb_new_success", 0) - tail_diag.get("tail_absorb_old_success", 0)) + max(0, low_diag.get("low_load_new_success", 0) - low_diag.get("low_load_old_success", 0))

            group_total = time.time() - group_start
            runtime = {"packing": pack_runtime["packing"], "topup": pack_runtime.get("topup", 0.0), "retry": pack_runtime["retry"], "repack": repack_time, "total": group_total, **rescue_timing}
            type_stats = ResultFormatter.build_type_stats(type_plan, pallet_type, sales_order_no, index_diag, rescued, runtime, repack, hf, tu, rb, low_diag, tail_diag, low_fill_diag, pool_diag)
            by_type_stats[f"{pallet_type}__{sales_order_no}"] = type_stats
            final_plan.extend(type_plan)
            runtime_stats["group_pack_seconds"] += pack_runtime["packing"]
            runtime_stats["group_topup_seconds"] += pack_runtime.get("topup", 0.0)
            runtime_stats["group_retry_seconds"] += pack_runtime["retry"]
            runtime_stats["group_repack_seconds"] += repack_time
            runtime_stats["group_total_seconds"] += group_total
            self._print_group_summary(type_stats, rescued, pack_runtime["packing"], pack_runtime["retry"], repack_time, group_total, pallet_type, sales_order_no, repack)

        self._cross_group_fill_compact(final_plan, by_type_stats)
        self._restore_split_orders(final_plan, by_type_stats)
        total_runtime = time.time() - start
        summary = {"overall": ResultFormatter.build_overall_summary(final_plan, by_type_stats, runtime_stats, total_runtime), "by_pallet_type": by_type_stats}
        self._print_overall(summary["overall"], runtime_stats, total_runtime)
        report = ResultFormatter.build_full_report(final_plan, summary, total_runtime, all_boxes, self._make_json_plan)
        if self._report_persister is not None:
            self._report_persister.persist(report, total_runtime)
        return report

    def _run_group_gcp(
        self, pallet_type, sales_order_no, boxes_in_group, target_mpm,
        group_start, final_plan, by_type_stats, runtime_stats,
    ) -> bool:
        """用全局列式装箱器处理一个分组并完成收尾（无救援链）。

        返回 True=GCP 结果有效、已收尾；False=盘数远超理论盘数（CP-SAT 装不满
        达标盘 → 残料摊成大量小盘），调用方应丢弃并回退 baseline beam，保证不比
        旧算法差。
        """
        type_plan, pack_runtime, index_diag = self._gcp_packer.pack_group(
            pallet_type, sales_order_no, boxes_in_group, target_mpm,
        )
        self._drop_empty_pallets(type_plan)
        # 爆盘回退：GCP 盘数远超理论盘数（高密度订单 CP-SAT 装不满 → 残料单柱
        # 兜底摊成大量小盘）时丢弃 GCP 结果，回退 baseline，防止比旧算法退步。
        # gcp_bailout = packer 内部体积下界已证明必超阈，提前止损返回（同语义更快）。
        total_mpm = sum(float(b.get('min_pack_multiple', 0) or 0) for b in boxes_in_group)
        theo = max(1, int(-(-total_mpm // target_mpm))) if target_mpm else 1
        if index_diag.get('gcp_bailout') or (
                target_mpm is not None and len(type_plan) > theo + 1):  # 残料容忍 1 盘，超则回退
            return False
        # GCP 无救援链，但失败盘同样要"尽量装满"：挂互借修复（合并重装，
        # 棘轮验收：守恒+门禁+盘数更少或新增达标才接受，结构上不退步）。
        t_repack = time.time()
        repack = self._call_rescue_optimizer(
            type_plan, target_mpm, boxes_in_group[0].get('pallet_dims'),
        )
        repack_time = time.time() - t_repack
        self._drop_empty_pallets(type_plan)
        rescued = repack.get("rescued", 0)
        group_total = time.time() - group_start
        runtime = {
            "packing": pack_runtime["packing"], "topup": 0.0, "retry": 0.0,
            "repack": repack_time, "total": group_total,
            "pair_repack_seconds": repack_time,
        }
        type_stats = ResultFormatter.build_type_stats(
            type_plan, pallet_type, sales_order_no, index_diag, rescued,
            runtime, repack,
        )
        by_type_stats[f"{pallet_type}__{sales_order_no}"] = type_stats
        final_plan.extend(type_plan)
        runtime_stats["group_pack_seconds"] += pack_runtime["packing"]
        runtime_stats["group_repack_seconds"] += repack_time
        runtime_stats["group_total_seconds"] += group_total
        self._print_group_summary(
            type_stats, rescued, pack_runtime["packing"], 0.0, repack_time,
            group_total, pallet_type, sales_order_no, repack,
        )
        return True

    def _partition_groups(self, grouped: Dict) -> Dict:
        """组内子聚类：把「规则子集 + 杂箱」混合的销售订单组拆成两个子组。

        regular 子集保留原订单号（下游 suits_group=True → 走 GCP 精确 ILP）；
        rest 杂箱用临时后缀子组（suits_group=False → 走 baseline + 救援链）。
        逐组循环、救援链、门禁完全不改。纯规则组 / 纯杂组不拆（零回归）。
        盘号/订单号的后缀由 _restore_split_orders 在输出前还原，对外不可见。
        """
        if self._gcp_packer is None:
            return grouped
        new_grouped: Dict = {}
        split_cnt = 0
        for (pallet_type, sales_order_no), boxes in grouped.items():
            target = self._targets.get(pallet_type)
            regular, rest = self._gcp_packer.partition_suitable(boxes, target)
            # 仅「主体规则(suits=True)+少量杂箱」才拆：主体本就适合 GCP，抽走
            # 少数杂箱让主体走精确 ILP。主体不规则的「大箱+伴层」型(suits=False)
            # 必须整组 baseline 做全局伴层配对，绝不拆——否则切断跨底面配对，严重
            # 回归（实测 5000 三组 suits=False，无脑拆 141→98）。
            if (regular and rest
                    and self._gcp_packer.suits_group(boxes, target)):
                new_grouped[(pallet_type, sales_order_no)] = regular
                new_grouped[(pallet_type, sales_order_no + _SPLIT_REST_TAG)] = rest
                split_cnt += 1
            else:
                new_grouped[(pallet_type, sales_order_no)] = boxes
        if split_cnt:
            print(f"  - 组内子聚类：{split_cnt} 个混合订单拆出规则子集走 GCP、杂箱走 baseline。")
        return new_grouped

    def _cross_group_fill_compact(
        self, final_plan: List[Dict], by_type_stats: Dict,
    ) -> None:
        """跨子组装满压实：只处理被组内子聚类拆开的真实订单。

        __SPLITREST__ 拆分是内部路由手段（规则子集走 GCP、杂箱走
        baseline），但救援链按内部子组运行——杂箱组的碎片失败盘与主组的
        可行搭档被人为隔开（如 fill 0.07 碎盘在 4 盘杂箱组里无搭档可并）。
        所有组完成后按"真实订单"重聚（case_group 标签保留在订单号里，
        业务隔离不破坏），对跨子组失败盘补一次装满压实（纯减盘，守恒+
        门禁同 `RescueOptimizer._fill_compact`），并同步受影响子组的统计。
        """
        if self._rescue_optimizer is None or not hasattr(
            self._rescue_optimizer, 'fill_compact'
        ):
            return
        groups: Dict = {}
        for p in final_plan:
            order = p.get('sales_order_no') or ''
            key = (p.get('pallet_type'), order.replace(_SPLIT_REST_TAG, ''))
            info = groups.setdefault(key, {'plans': [], 'orders': set()})
            info['plans'].append(p)
            info['orders'].add(order)
        from src.rescue.pallet_evaluator import PalletEvaluator
        for (pallet_type, real_order), info in groups.items():
            if len(info['orders']) < 2:
                continue   # 未拆分：组内压实已做过，不重复
            target_mpm = self._targets.get(pallet_type)
            if target_mpm is None:
                continue
            plans = info['plans']
            failed = [
                p for p in plans
                if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
            ]
            if len(failed) < 2:
                continue
            dims = None
            for p in plans:
                items = p.get('packed_items') or []
                if items and items[0].get('pallet_dims'):
                    dims = items[0]['pallet_dims']
                    break
            if not dims:
                continue
            sub = list(plans)
            diag = self._rescue_optimizer.fill_compact(
                sub, float(target_mpm), pallet_dims=dims
            )
            if not diag.get("fill_compact_merges"):
                continue
            print(
                f"  - 跨子组装满压实（{pallet_type} / {real_order}）："
                f"合并 {diag['fill_compact_merges']} 次（纯减盘数）。"
            )
            # 原组托盘整体替换（保持组首位置，其余组次序不动）
            group_ids = {id(p) for p in plans}
            idx = next(
                i for i, p in enumerate(final_plan) if id(p) in group_ids
            )
            rest = [p for p in final_plan if id(p) not in group_ids]
            final_plan.clear()
            final_plan.extend(rest[:idx] + sub + rest[idx:])
            # 受影响子组统计同步（总汇总按 by_type_stats 累加，必须一致）
            for internal_order in info['orders']:
                st = by_type_stats.get(f"{pallet_type}__{internal_order}")
                if not st:
                    continue
                plans_o = [
                    p for p in sub
                    if p.get('sales_order_no') == internal_order
                ]
                st.update(PalletEvaluator.recompute_type_stats(plans_o))
                diag_d = st.get('diagnosis')
                if isinstance(diag_d, dict):
                    fills = [
                        float(p.get('fill_rate') or 0.0)
                        for p in plans_o if p.get('packed_items')
                    ]
                    low = [
                        p for p in plans_o
                        if p.get('mpm_status') == 'FAILED'
                        and float(p.get('fill_rate') or 0.0) < 0.3
                    ]
                    diag_d['low_fill_failed_pallets'] = len(low)
                    diag_d['algorithm_underfilled_order'] = bool(low)
                    diag_d['avg_fill_rate'] = round(
                        sum(fills) / max(1, len(fills)), 6
                    )

    def _restore_split_orders(self, final_plan: List[Dict], by_type_stats: Dict) -> None:
        """输出前统一盘号与订单号：还原内部订单后缀（组内子聚类
        __SPLITREST__ / case_group 分组标签），case_group 写回盘级字段；
        盘号**无条件**按 (托盘类型, 订单) 顺序重编为 `类型-订单-序号`——
        救援阶段的临时号（如 -RC1）与合并/丢盘产生的断号一律归一，
        保证所有报告的 pallet_id 格式一致且组内连续。"""
        def _tagged(order: str) -> bool:
            return _SPLIT_REST_TAG in order or CASE_GROUP_ORDER_TAG in order

        if any(_tagged(p.get('sales_order_no') or '') for p in final_plan):
            for p in final_plan:
                o = (p.get('sales_order_no') or '').replace(_SPLIT_REST_TAG, '')
                clean, cg = split_case_group_tag(o)
                if cg:
                    p['case_group'] = cg
                p['sales_order_no'] = clean
            for st in by_type_stats.values():
                o = (st.get('sales_order_no') or '').replace(_SPLIT_REST_TAG, '')
                clean, cg = split_case_group_tag(o)
                if cg:
                    st['case_group'] = cg
                st['sales_order_no'] = clean
        seq: Dict = {}
        for p in final_plan:
            k = (p.get('pallet_type'), p.get('sales_order_no'))
            seq[k] = seq.get(k, 0) + 1
            p['pallet_id'] = f"{k[0]}-{k[1]}-{seq[k]}"

    def _call_rescue_optimizer(
        self, type_plan: List[Dict], target_mpm: Optional[float],
        pallet_dims: Optional[Dict],
    ) -> Dict:
        """互借修复调用：显式传托盘尺寸（与其它救援器同源），兼容旧签名注入。

        未合并时打印原因行，避免静默失败难排查。
        """
        fn = self._rescue_optimizer.optimize_failed_by_failed
        try:
            supports_dims = 'pallet_dims' in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            supports_dims = False
        diag = (
            fn(type_plan, target_mpm, pallet_dims=pallet_dims)
            if supports_dims else fn(type_plan, target_mpm)
        ) or {}
        reason = diag.get("consolidate_reason", "")
        if reason and reason != "ok":
            print(f"  - 失败托盘互借诊断：未合并（{reason}）。")
        r_reason = diag.get("redistribute_reason", "")
        if r_reason and r_reason not in ("ok", "no_failed_pool",
                                         "no_low_fill_success_donor"):
            print(f"  - 指数再分配诊断：未接受（{r_reason}）。")
        return diag

    def _drop_empty_pallets(self, type_plan: List[Dict]) -> int:
        before = len(type_plan)
        type_plan[:] = [plan for plan in type_plan if plan.get('packed_items')]
        return before - len(type_plan)

    def _print_diagnostics(self, diag: Dict, target_mpm: Optional[float]) -> None:
        if target_mpm is None:
            return
        canonical = (diag.get("canonical_layer_best") or {}).get("best_mpm")
        print(f"  - 指数诊断：箱子 {diag['box_count']} 个，总指数 {diag['total_mpm']:g}，目标 {target_mpm:g}，理论最多达标托盘 {diag['theoretical_success_pallets']} 个，剩余指数 {diag['residual_mpm']:g}。")
        if canonical is not None:
            tail = ' 当前目标可能受托盘高度/箱型组合限制。' if canonical < target_mpm else ''
            print(f"  - 几何诊断：典型整层堆叠单盘参考上限 {canonical:g}/{target_mpm:g}。{tail}")

    def _print_group_summary(self, type_stats, rescued, pack_t, retry_t, repack_t, total_t, pallet_type, sales_order_no, repack) -> None:
        kpi = type_stats["kpi"]
        runtime = type_stats.get("runtime_breakdown_seconds", {})
        print(f"托盘类型 {pallet_type}（销售订单号：{sales_order_no}）处理完成：总托盘 {type_stats['total_pallets']}，指数达标 {type_stats['success_pallets']}，未达标 {type_stats['failed_pallets']}，平均缺口 {type_stats['avg_mpm_gap']:.2f}，最大缺口 {type_stats['max_mpm_gap']:.2f}，失败托盘互借修复成功 {rescued}。")
        print(f"  - KPI：near/mid/deep={kpi['failed_near_count']}/{kpi['failed_mid_count']}/{kpi['failed_deep_count']}，救回率={kpi['rescue_rate_from_failed']:.2%}，pair效率={kpi['pair_efficiency']:.2%}。")
        print(f"  - 耗时拆解：packing={pack_t:.2f}s，main_topup={runtime.get('topup', 0.0):.2f}s，retry={retry_t:.2f}s，failed_pool={runtime.get('failed_pool', 0.0):.2f}s，repack={repack_t:.2f}s，total={total_t:.2f}s。\n")

    def _print_overall(self, overall, runtime_stats, total_runtime):
        kpi = overall["kpi"]
        print("=" * 40)
        print(f"统计汇总：总托盘 {overall['total_pallets']}，指数达标 {overall['success_pallets']}，未达标 {overall['failed_pallets']}，未知 {overall['unknown_pallets']}，平均缺口 {overall['avg_mpm_gap']:.2f}，最大缺口 {overall['max_mpm_gap']:.2f}，失败托盘互借修复成功 {overall['rescued_from_failed']}")
        print(f"KPI汇总：near/mid/deep={kpi['failed_near_count']}/{kpi['failed_mid_count']}/{kpi['failed_deep_count']}，pair尝试={kpi['pair_tried']}，pair改进={kpi['pair_improved']}，pair效率={kpi['pair_efficiency']:.2%}")
        print(f"耗时汇总：packing={runtime_stats['group_pack_seconds']:.2f}s，main_topup={runtime_stats.get('group_topup_seconds', 0.0):.2f}s，retry={runtime_stats['group_retry_seconds']:.2f}s，repack={runtime_stats['group_repack_seconds']:.2f}s，end_to_end={total_runtime:.2f}s")
        print("=" * 40)
