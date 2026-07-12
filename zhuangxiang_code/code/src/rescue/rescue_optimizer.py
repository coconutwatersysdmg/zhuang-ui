"""
救援优化器（失败托盘互借修复 + 指数互换）

负责失败托盘之间的箱子互借与整体合并：把组内全部 FAILED 托盘的箱子并成
自由箱池，先做**凑标阶段**（在失败池上重跑配方规划，把凑得出目标指数的
箱子子集提成达标盘），再逐盘重装剩余箱（装满优先），使失败托盘
"更多达标、更少、更满"。超预算时降级快速收尾，已凑出的达标盘绝不丢弃。

之后做**指数再分配**：低填充达标盘（填充 70% 就达标 = 高指数密度箱被
错配集中）溶解进池，和失败盘的低指数箱一起重跑配方规划；廉价预判
（规划实例数净增才开工）+ 独立棘轮（新达标数超过溶解数才接受）。

互借之后再做**成功盘↔失败盘指数互换**（src/rescue/index_swap.py）：
把富余成功盘（mpm > target）的高指数箱与失败盘低指数箱互换，成功盘
保持达标、失败盘凑到目标。

验收采用严格棘轮：箱子守恒 + 全量约束门禁 + （失败盘数变少 或 新增达标盘）
三者同时满足才替换原方案，否则原样保留——结构上保证不退步。
"""

import time
from typing import Callable, Dict, List, Optional

from src.geometry.constraint_validator import validate_pallet_constraints
from src.packing.direct_layer_packer import (
    build_centered_single_box_solution,
    build_direct_layer_packing_solution,
)
from src.packing.pool_compactor import PoolCompactor
from src.rescue.index_swap import swap_success_failed
from src.rescue.pallet_evaluator import PalletEvaluator
from src.utils.helpers import repack_ready_item


class RescueOptimizer:
    """失败托盘互借修复：合并重装失败盘，装满优先、达标顺带。"""

    #: 单组参与合并的箱子上限（安全阀，防超大组耗时失控；超限时优先
    #: 选填充率最低的失败盘进池，装得较满的失败盘原样保留）
    MAX_POOL_BOXES = 2000
    #: 已经"够满"的失败盘不进池（重装它们既费时也难有收益）
    FILL_KEEP_THRESHOLD = 0.92
    #: 单组互借修复（合并重装）的墙钟预算（秒）；超时降级快速收尾，
    #: 已凑出的达标盘绝不丢弃（慢机器只损失装满质量、不损失达标数）
    CONSOLIDATION_TIME_BUDGET_S = 90.0
    #: 指数再分配：填充率低于此值的达标盘可"溶解"进池参与重规划。
    #: 低填充就达标 = 高指数密度箱被错配集中的信号；把它们和失败盘的
    #: 低指数箱一起重规划，常能多凑出达标盘。棘轮保证溶解的达标数
    #: 绝不净损失（新达标数不低于溶解数才接受）。
    REDISTRIBUTE_FILL_MAX = 0.80
    #: 指数再分配单组最多溶解的达标盘数（控制规划规模与风险敞口）
    REDISTRIBUTE_MAX_DONORS = 4
    #: 指数再分配阶段独立墙钟预算（秒）；廉价预判通过后才会花这笔钱
    REDISTRIBUTE_TIME_BUDGET_S = 60.0

    def __init__(
        self,
        pallet_dims: Dict[str, float],
        enable_expensive_repack: bool = False,
        custom_packer_cls=None,
        validate_center_of_mass: Optional[Callable] = None,
        constraint_config=None,
    ):
        """
        Args:
            pallet_dims: 托盘尺寸 {'length','width','height'}。
            enable_expensive_repack: 兼容保留（历史开关，现无高耗时分支）。
            custom_packer_cls: 真实装箱器类（BeamSearchPacker）；缺省时
                互借修复退化为只做诊断（与历史空实现行为一致）。
            validate_center_of_mass: 重心校验函数（与其它救援器同源注入）。
            constraint_config: 约束统一配置；None 时用内置默认。
        """
        if constraint_config is None:
            from src.config.constraint_config import ConstraintConfig
            constraint_config = ConstraintConfig()
        self.pallet_dims = pallet_dims
        self.enable_expensive_repack = enable_expensive_repack
        self._CustomPacker = custom_packer_cls
        self._validate_com = validate_center_of_mass
        self._cfg = constraint_config

    def optimize_failed_by_failed(
        self,
        type_plans: List[Dict],
        target_mpm: Optional[float],
        pallet_dims: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """失败托盘互借修复（合并重装）。

        流程：
        1. 挑出组内 FAILED 且未"够满"的托盘（填充率 < FILL_KEEP_THRESHOLD）；
        2. 全部箱子还原为可重装状态并成池；
        3. 用真实装箱器逐盘重装（多层、装满优先，target 传入以便凑指数）；
        4. 棘轮验收：箱子守恒 + 新盘逐盘全量门禁 +
           （盘数更少 或 新增达标）→ 原地替换，否则不动。

        Args:
            type_plans: 某 (托盘类型, 订单) 分组的全部托盘方案（原地修改）。
            target_mpm: 目标指数；None 时跳过。
            pallet_dims: 托盘尺寸；提供时覆盖构造期尺寸（workflow 显式传入，
                与其它救援器同源，避免从箱子字段推断失败）。

        Returns:
            诊断 dict：rescued（新增达标盘数）、consolidate_*（合并明细）、
            以及兼容历史统计口径的 pair_*/tier_counts 等字段。
        """
        if pallet_dims:
            self.pallet_dims = pallet_dims
        diag = {
            "rescued": 0,
            "pair_tried": 0,
            "pair_improved": 0,
            "pair_no_improve": 0,
            "pair_pack_fail": 0,
            "tier_counts": {"near": 0, "mid": 0, "deep": 0},
            "filtered_out_candidates": 0,
            "pair_budget": 0,
            "fallback_triggered": False,
            "consolidate_tried": 0,
            "consolidate_accepted": 0,
            "consolidate_old_pallets": 0,
            "consolidate_new_pallets": 0,
            "consolidate_new_success": 0,
            "consolidate_old_avg_fill": 0.0,
            "consolidate_new_avg_fill": 0.0,
            "consolidate_reason": "",
            "extract_commit_success": 0,
            "redistribute_tried": 0,
            "redistribute_accepted": 0,
            "redistribute_dissolved": 0,
            "redistribute_reason": "",
            "fill_compact_merges": 0,
            "fill_compact_reason": "",
            "swap_tried": 0,
            "swap_accepted": 0,
            "swap_surplus_total": 0.0,
            "swap_reason": "",
        }
        if target_mpm is None or len(type_plans) < 2:
            diag["consolidate_reason"] = "no_target_or_single_plan"
            return diag

        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)

        failed = [
            p for p in type_plans
            if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
        ]
        for p in failed:
            gap = max(0.0, float(p.get('mpm_gap') or 0.0))
            tier = "near" if gap <= 8 else ("mid" if gap <= 24 else "deep")
            diag["tier_counts"][tier] += 1

        if self._CustomPacker is None:
            diag["consolidate_reason"] = "no_packer_injected"
            return diag
        if self._pallet_volume() <= 0:
            diag["consolidate_reason"] = "no_pallet_dims"
            return diag

        # 指数互换先行：原始失败盘尚未被重装压密，近缺口盘还有体积余量
        # 接收高指数箱；换完的达标盘自然退出后续互借池。
        swap_diag = swap_success_failed(
            type_plans, float(target_mpm), self.pallet_dims,
            self._CustomPacker, self._validate_com, self._cfg,
        )
        diag.update(swap_diag)
        diag["rescued"] += swap_diag.get("swap_accepted", 0)
        if swap_diag.get("swap_accepted", 0) > 0:
            print(
                f"  - 指数互换救援：成功盘↔失败盘互换 "
                f"{swap_diag['swap_accepted']} 次，新增达标 "
                f"{swap_diag['swap_accepted']}"
                f"（组内富余指数 {swap_diag['swap_surplus_total']:.1f}）。"
            )
            failed = [
                p for p in type_plans
                if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
            ]

        self._consolidate(type_plans, float(target_mpm), failed, diag)
        return diag

    def _consolidate(
        self,
        type_plans: List[Dict],
        target_mpm: float,
        failed: List[Dict],
        diag: Dict,
    ) -> None:
        """失败盘合并重装编排：三阶段独立棘轮，互不连坐。

        阶段一（凑标局部提交）：失败池上重跑配方规划，凑出的达标盘**立即
        局部提交**——实例箱子重映射到尽量少的源托盘（同规格箱可互换），
        只重排受影响盘的剩余箱。几秒级、与机器速度无关，达标增益先落袋。
        阶段二（合并装满）：剩余失败盘纯合并重装（不再凑标），预算内尽力；
        超时/碎盘被棘轮拒绝也不影响阶段一已提交的达标盘。
        阶段三（指数再分配）：低填充达标盘溶解重规划（独立预判 + 棘轮）。
        阶段四（装满压实）：残余失败盘两两合并（2 盘→1 盘）逐对提交——
        整池合并整体被拒时的兜底，保证碎片盘（如 fill 0.07/0.14）不残留。

        历史缺陷（2026-07-10 修复）：凑标与全池合并绑在同一个候选里做
        全有全无验收，合并重装在预算刀口上（同机 78s vs 95s 冷热波动、慢
        机器必超时）→ 降级碎盘被棘轮拒绝时把凑标成果连坐丢弃，达标数随
        机少 2-3。拆开后凑标增益结构上不再受合并耗时影响。
        """
        selected = self._select_pool_pallets(failed)
        if len(selected) >= 2:
            self._extract_commit(type_plans, target_mpm, selected, diag)
            failed_now = [
                p for p in type_plans
                if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
            ]
            selected2 = self._select_pool_pallets(failed_now)
            if len(selected2) >= 2:
                t0 = time.time()
                self._consolidate_once(
                    type_plans, target_mpm, selected2, 0,
                    t0 + self.CONSOLIDATION_TIME_BUDGET_S, diag, t0,
                    prefix="consolidate", label="失败托盘互借修复",
                    extract=False,
                )
            else:
                diag["consolidate_reason"] = "less_than_2_consolidatable"
        else:
            diag["consolidate_reason"] = "less_than_2_consolidatable"

        self._redistribute(type_plans, target_mpm, diag)
        self._fill_compact(type_plans, target_mpm, diag)

    #: 凑标局部提交阶段的墙钟预算（秒）：规划 + 实例实装 + 受影响盘重排
    EXTRACT_COMMIT_BUDGET_S = 60.0
    #: 装满压实（两两合并兜底）的墙钟预算（秒）；每对合并秒级，
    #: 预算主要防御病态多盘组
    FILL_COMPACT_BUDGET_S = 20.0
    #: 吸收溶解每轮最多尝试的搭档盘数（最空者优先；再多的搭档更满、
    #: 更吸不动，只会烧 beam 时间）
    FILL_COMPACT_ABSORB_PARTNERS = 6

    def fill_compact(
        self,
        type_plans: List[Dict],
        target_mpm: Optional[float],
        pallet_dims: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """公开入口：只跑装满压实阶段（workflow 跨子组收尾用）。

        组内子聚类把同一真实订单拆成多个内部子组时，各子组救援链互相
        看不见对方的失败盘；所有组完成后 workflow 按真实订单重聚，用本
        入口对跨子组失败盘补一次纯装满合并。语义同 `_fill_compact`
        （守恒 + 门禁 + 严格减盘，达标盘不动）。

        Returns:
            诊断 dict：fill_compact_merges / fill_compact_reason。
        """
        if pallet_dims:
            self.pallet_dims = pallet_dims
        diag = {"fill_compact_merges": 0, "fill_compact_reason": ""}
        if target_mpm is None or self._CustomPacker is None \
                or self._pallet_volume() <= 0:
            diag["fill_compact_reason"] = "no_target_or_packer"
            return diag
        for plan in type_plans:
            PalletEvaluator.calc_pallet_status(plan)
        self._fill_compact(type_plans, float(target_mpm), diag)
        return diag

    def _fill_compact(
        self,
        type_plans: List[Dict],
        target_mpm: float,
        diag: Dict,
    ) -> None:
        """装满压实兜底：残余失败盘两两合并（2 盘→1 盘），逐对局部提交。

        阶段二的整池合并是全有全无验收——超时降级碎盘、或"+1 达标"分支
        通过后残留碎片盘时，明显可合并的低填充盘（如 fill 0.07 + 0.50）会
        原样留下。本阶段只做纯装满，两条通道逐一尝试最空盘：
        1. 两两合并（2 盘→1 盘）：找体积上装得下它全部箱子的搭档盘，
           PoolCompactor / 单盘重装一次合并；
        2. 吸收溶解：单一搭档装不下整盘时（如碎盘是几个大箱、各搭档只剩
           零散空间），把最空盘的箱子**分摊**进多个搭档盘——每个搭档盘
           连同待吸收箱重装一盘，要求搭档原箱全保留且至少吸收 1 箱；
           全部箱子被吸收才提交（盘数-1），否则原样保留。
        守恒 + 全量门禁通过才提交，逐盘独立，任何一次失败不影响其它。
        不产生新达标（合并盘仍 FAILED），纯减盘数、提填充。
        """
        deadline = time.time() + self.FILL_COMPACT_BUDGET_S
        pv = self._pallet_volume()
        if pv <= 0:
            return
        merges = 0
        skipped: set = set()   # 两条通道都失败的盘：本轮不再重试
        while time.time() < deadline:
            failed = [
                p for p in type_plans
                if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
                and self._fill_rate(p) < self.FILL_KEEP_THRESHOLD
                and id(p) not in skipped
            ]
            if len(failed) < 2:
                break
            failed.sort(
                key=lambda p: (self._fill_rate(p), str(p.get('pallet_id')))
            )
            emptiest = failed[0]
            ev = self._items_volume(emptiest.get('packed_items', []))
            # 通道 1：能在体积上容下最空盘全部箱子的搭档。最空者优先——
            # 合并成功都是净减 1 盘（收益相同），最空搭档几何上最可能装下
            # （首个搭档即命中 → 秒级、与机器速度无关），且合并盘越空
            # 后续轮次越可能继续链式吸收。合并盘填充上限取
            # FILL_KEEP_THRESHOLD：混箱单盘超过它实际装不出，白试。
            partners = [
                p for p in failed[1:]
                if self._items_volume(p.get('packed_items', [])) + ev
                <= pv * self.FILL_KEEP_THRESHOLD
            ]
            merged = False
            for partner in sorted(
                partners,
                key=lambda p: (self._fill_rate(p), str(p.get('pallet_id'))),
            ):
                if time.time() > deadline:
                    break
                if self._merge_pair(
                    type_plans, emptiest, partner, target_mpm
                ):
                    merges += 1
                    merged = True
                    break
            # 通道 2：分摊吸收（搭档 = 组内其余全部失败盘，最空者先试）。
            # 只对真碎盘（fill < 尾盘阈值）开：把中填充盘整盘摊进别人
            # 需要的富余空间几乎不存在，白烧 beam 时间。
            if (not merged and time.time() < deadline
                    and self._fill_rate(emptiest)
                    < self.TAIL_FILL_THRESHOLD):
                merged = self._absorb_dissolve(
                    type_plans, emptiest, failed[1:], target_mpm, deadline
                )
                if merged:
                    merges += 1
            if not merged:
                skipped.add(id(emptiest))  # 换下一个最空盘，不重复空转
        diag["fill_compact_merges"] = merges
        diag["fill_compact_reason"] = "ok" if merges else "no_mergeable_pair"
        if merges:
            print(f"  - 失败盘装满压实：合并/吸收 {merges} 次（纯减盘数）。")

    def _cpsat_column_merge(self, pool: List[Dict]) -> List[Dict]:
        """CP-SAT 精确摆柱把整池装进一盘（复用 GCP 落地原语）。

        贪心行式摆放对"大底面混柱"常摆不开（行宽取最大柱宽浪费严重），
        CP-SAT 二维精确布局能开。凑柱 → 精确摆柱 → _assemble 写坐标/
        吸盘字段（与 GCP 落地同源，gap=0 紧贴）。装不全返回 []。
        短时限（3s）：本阶段是兜底通道，秒级/对是底线。
        """
        # 惰性导入，避免 rescue <-> packing 模块环
        from src.packing.global_column_packer import (
            _build_columns, _center_placed, _cpsat_pack_2d,
        )
        from src.packing.layered_packer import _assemble

        cols = _build_columns(pool, self.pallet_dims)
        if not cols:
            return []
        ph = float(self.pallet_dims.get('height', 0) or 0)
        for c in cols:
            col_h = sum(
                float(b.get('height', 0) or 0) for b in c['boxes']
            )
            if col_h > ph + 1e-9:
                return []   # 凑柱超高（防御性，源盘箱本应合法）
        # 面积预判：柱底面积和已超盘面 → 全放必不可行，不必起 CP-SAT
        #（本通道要求 unplaced==0，面积是它的硬下界）
        plate_area = (
            float(self.pallet_dims.get('length', 0) or 0)
            * float(self.pallet_dims.get('width', 0) or 0)
        )
        if sum(c['xlen'] * c['ylen'] for c in cols) > plate_area + 1e-6:
            return []
        placed, unplaced = _cpsat_pack_2d(
            cols, self.pallet_dims, time_limit=3.0
        )
        if unplaced or not placed:
            return []
        placed = _center_placed(placed, self.pallet_dims, 2.0)
        helper = self._CustomPacker(
            self.pallet_dims, constraint_config=self._cfg
        )
        return _assemble(placed, helper, self.pallet_dims, gap=0.0)

    def _absorb_dissolve(
        self,
        type_plans: List[Dict],
        emptiest: Dict,
        partners: List[Dict],
        target_mpm: float,
        deadline: float,
    ) -> bool:
        """吸收溶解：最空盘的箱子分摊进多个搭档盘，整盘清空才提交。

        单一搭档盘装不下碎盘全部箱子时（典型：碎盘是几个大底面箱，各搭档
        只剩零散空间），逐个搭档重装"搭档原箱 + 剩余待吸收箱"：搭档原箱
        必须全保留、且至少吸收 1 箱才算有效。所有箱子被吸收后统一走守恒 +
        门禁提交（盘数-1）；任何箱子落单则整体放弃、原方案不动。
        """
        remaining = [
            repack_ready_item(i)
            for i in emptiest.get('packed_items', [])
        ]
        if not remaining:
            return False
        # (partner, new_packed)；同一搭档可多轮吸收（后轮覆盖前轮结果）
        latest: Dict[int, tuple] = {}
        current_items = {
            id(p): [repack_ready_item(i) for i in p.get('packed_items', [])]
            for p in partners
        }
        # 多轮：一个搭档吃不下整盘时分几轮摊（首轮 A 吸一部分后，剩余
        # 变少，此前吃不下的 B 下一轮可能就吃得下），无进展即停
        rounds = 0
        while remaining and time.time() < deadline and rounds < 3:
            rounds += 1
            progressed = False
            for partner in sorted(
                partners,
                key=lambda p: (self._fill_rate(p), str(p.get('pallet_id'))),
            )[:self.FILL_COMPACT_ABSORB_PARTNERS]:
                if not remaining or time.time() > deadline:
                    break
                base = current_items[id(partner)]
                if len(base) + len(remaining) > 120:
                    continue   # beam 大池会烧穿预算，秒级/盘是本阶段底线
                base_ids = {i.get('id') for i in base}
                pool = sorted(
                    base + remaining,
                    key=lambda x: (
                        -self._items_volume([x]), str(x.get('id'))
                    ),
                )
                packed = self._pack_one_pallet(pool, target_mpm, seed=52201)
                if not packed:
                    continue
                placed_ids = {i.get('id') for i in packed}
                if not base_ids <= placed_ids:
                    continue   # 搭档原箱必须全保留（吸收不能挤掉别人）
                if not placed_ids - base_ids:
                    continue
                latest[id(partner)] = (partner, packed)
                current_items[id(partner)] = [
                    repack_ready_item(i) for i in packed
                ]
                remaining = [
                    b for b in remaining if b.get('id') not in placed_ids
                ]
                progressed = True
            if not progressed:
                break
        if remaining:
            return False   # 没吸干净 → 整体放弃（盘数不减就没意义）
        updates = list(latest.values())
        selected = [emptiest] + [p for p, _ in updates]
        rebuilt_sets = [packed for _, packed in updates]
        original_ids = {
            i.get('id') for p in selected for i in p.get('packed_items', [])
        }
        rebuilt_ids = {i.get('id') for s in rebuilt_sets for i in s}
        if rebuilt_ids != original_ids:
            return False
        candidate_plans, new_plans = self._build_candidate_plans(
            type_plans, selected, rebuilt_sets, target_mpm
        )
        for solution in new_plans:
            gate = validate_pallet_constraints(
                solution, self.pallet_dims, constraint_config=self._cfg,
                target_mpm=target_mpm,
            )
            if not gate["is_valid"]:
                return False
        type_plans.clear()
        type_plans.extend(candidate_plans)
        return True

    def _merge_pair(
        self,
        type_plans: List[Dict],
        a: Dict,
        b: Dict,
        target_mpm: float,
    ) -> bool:
        """把两个失败盘合并为一盘：守恒 + 门禁通过才提交，否则不动。"""
        pool = [
            repack_ready_item(item)
            for p in (a, b) for item in p.get('packed_items', [])
        ]
        original_ids = {i.get('id') for i in pool}
        if len(original_ids) != len(pool):
            return False
        packed_sets: List[List[Dict]] = []
        if len(pool) <= 120:
            compact = PoolCompactor(
                self.pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                constraint_config=self._cfg,
            ).compact(pool, max_pallets=1)
            if compact["success"]:
                packed_sets = compact["packed_sets"]
        if not packed_sets:
            # 贪心行式列摆放装不下 → CP-SAT 精确摆柱（大底面混柱贪心常
            # 摆不开、精确解能开，如 2 个大箱碎盘 + 12 箱半满盘）。
            # 内含柱底面积预判：面积注定放不全时零成本返回。
            packed = self._cpsat_column_merge(pool)
            if packed and {i.get('id') for i in packed} == original_ids:
                packed_sets = [packed]
        if not packed_sets and len(pool) <= 60:
            # 整层格栅 + beam 择优再试一盘。只对小池兜底：beam 逐对烧秒级
            # 时间，大池合并本就该由列式/CP-SAT 判定。
            packed = self._pack_one_pallet(
                sorted(
                    pool,
                    key=lambda x: (
                        -self._items_volume([x]), str(x.get('id'))
                    ),
                ),
                target_mpm, seed=52201,
            )
            if packed and {i.get('id') for i in packed} == original_ids:
                packed_sets = [packed]
        if len(packed_sets) != 1:
            return False
        if {i.get('id') for s in packed_sets for i in s} != original_ids:
            return False
        candidate_plans, new_plans = self._build_candidate_plans(
            type_plans, [a, b], packed_sets, target_mpm
        )
        for solution in new_plans:
            gate = validate_pallet_constraints(
                solution, self.pallet_dims, constraint_config=self._cfg,
                target_mpm=target_mpm,
            )
            if not gate["is_valid"]:
                return False
        type_plans.clear()
        type_plans.extend(candidate_plans)
        return True

    def _extract_commit(
        self,
        type_plans: List[Dict],
        target_mpm: float,
        selected: List[Dict],
        diag: Dict,
    ) -> bool:
        """凑标局部提交：凑出的达标盘立即落袋，只重排受影响的源托盘。

        实例箱子先重映射到尽量少的源托盘（同规格箱互换，见
        `_cluster_instance_boxes`），受影响盘的剩余箱单独重排——池小、
        秒级完成，不与全池合并共命运。棘轮（守恒 + 门禁 + 新增达标 +
        失败盘数不增）不过则原样保留。

        Returns:
            True = 已提交；False = 无可凑/被拒（原方案未动）。
        """
        pool = [
            repack_ready_item(item)
            for p in selected
            for item in p.get('packed_items', [])
        ]
        if self._sum_mpm(pool) + 1e-9 < float(target_mpm):
            return False
        id2plan = {
            item.get('id'): id(p)
            for p in selected for item in p.get('packed_items', [])
        }
        t0 = time.time()
        deadline = t0 + self.EXTRACT_COMMIT_BUDGET_S

        def remap(inst, remaining):
            return self._cluster_instance_boxes(inst, remaining, id2plan)

        target_sets, _rest = self._extract_target_sets(
            pool, target_mpm, deadline, remap=remap
        )
        if not target_sets:
            return False

        stolen = {i.get('id') for packed in target_sets for i in packed}
        affected = [
            p for p in selected
            if any(i.get('id') in stolen for i in p.get('packed_items', []))
        ]
        leftover = [
            repack_ready_item(i)
            for p in affected for i in p.get('packed_items', [])
            if i.get('id') not in stolen
        ]
        leftover_rebuilt = self._repack_pool(
            leftover, target_mpm,
            expected_count=len(affected),
            prior_success=len(target_sets),
            deadline=deadline,
        )
        if leftover_rebuilt is None:
            return False
        rebuilt_sets = target_sets + leftover_rebuilt
        original_ids = {
            i.get('id') for p in affected for i in p.get('packed_items', [])
        }
        rebuilt_ids = {i.get('id') for s in rebuilt_sets for i in s}
        if rebuilt_ids != original_ids:
            return False

        new_success = sum(
            1 for s in rebuilt_sets
            if self._sum_mpm(s) + 1e-9 >= float(target_mpm)
        )
        new_failed = len(rebuilt_sets) - new_success
        # 棘轮：必须净增达标（受影响盘全为 FAILED）且失败盘数不增加
        if not (new_success > 0 and new_failed <= len(affected)):
            return False

        candidate_plans, new_plans = self._build_candidate_plans(
            type_plans, affected, rebuilt_sets, target_mpm
        )
        for solution in new_plans:
            gate = validate_pallet_constraints(
                solution, self.pallet_dims, constraint_config=self._cfg,
                target_mpm=target_mpm,
            )
            if not gate["is_valid"]:
                return False
        type_plans.clear()
        type_plans.extend(candidate_plans)
        diag["rescued"] += new_success
        diag["extract_commit_success"] = new_success
        print(
            f"  - 失败托盘互借修复（凑标提交）：新增达标 {new_success}，"
            f"受影响 {len(affected)} 盘重排为 {len(rebuilt_sets)} 盘"
            f"（其余失败盘未动），耗时 {time.time() - t0:.1f}s。"
        )
        return True

    def _cluster_instance_boxes(
        self,
        inst: List[Dict],
        remaining: List[Dict],
        id2plan: Dict,
    ) -> List[Dict]:
        """把配方实例的箱子重映射到尽量少的源托盘（同规格箱可互换）。

        配方只关心箱型与数量；同(尺寸/重量/指数/小箱标记)的箱子互换不影响
        实例可装性。贪心：每轮选"还能供给最多所需箱"的源托盘取箱，直至
        配齐。凑标因此只"打散"极少数源托盘，其余失败盘保持原样——这是
        局部提交能秒级完成、且失败盘数不膨胀的关键。配不齐（防御性）时
        退回原实例。
        """
        def tkey(b: Dict):
            return (
                round(float(b.get('length', 0) or 0), 1),
                round(float(b.get('width', 0) or 0), 1),
                round(float(b.get('height', 0) or 0), 1),
                round(float(b.get('weight', 0) or 0), 3),
                float(b.get('min_pack_multiple', 0) or 0),
                bool(b.get('is_small_box')),
            )

        need: Dict = {}
        for b in inst:
            k = tkey(b)
            need[k] = need.get(k, 0) + 1
        by_plan: Dict = {}
        for b in remaining:
            pid = id2plan.get(b.get('id'))
            by_plan.setdefault(pid, {}).setdefault(tkey(b), []).append(b)

        chosen: List[Dict] = []
        need_left = dict(need)
        while need_left:
            best_pid, best_gain = None, 0
            for pid, types in by_plan.items():
                gain = sum(
                    min(len(types.get(k, ())), n)
                    for k, n in need_left.items()
                )
                if gain > best_gain:
                    best_pid, best_gain = pid, gain
            if best_pid is None or best_gain == 0:
                return inst  # 配不齐 → 退回原实例（防御性，理论不可达）
            types = by_plan[best_pid]
            for k in list(need_left.keys()):
                boxes = types.get(k)
                if not boxes:
                    continue
                take = min(len(boxes), need_left[k])
                for _ in range(take):
                    chosen.append(boxes.pop())
                need_left[k] -= take
                if need_left[k] == 0:
                    del need_left[k]
            del by_plan[best_pid]
        return chosen

    def _redistribute(
        self,
        type_plans: List[Dict],
        target_mpm: float,
        diag: Dict,
    ) -> None:
        """指数再分配：溶解低填充达标盘 + 失败盘 → 配方重规划。

        针对"填充率 70% 就达标"的盘（高指数密度小箱被错配集中）：把它们
        拆散，和失败盘的低指数箱一起重跑配方规划——高密度箱型给整层枚举
        提供新的组合选择（如高密度层×1 + 低密度层×2 = 达标），同样的总
        指数凑出更多达标盘。

        廉价预判开关：plan_recipe_pools 只做规划不实装（秒级）；
        规划实例数(溶解池) - 溶解数 > 规划实例数(纯失败池) 才真正开工。
        棘轮基线 = 溶解数：新达标数超过基线才接受，溶解绝不净亏。
        """
        failed_now = [
            p for p in type_plans
            if p.get('mpm_status') == 'FAILED' and p.get('packed_items')
        ]
        selected = self._select_pool_pallets(failed_now)
        if not selected:
            diag["redistribute_reason"] = "no_failed_pool"
            return
        donors = self._select_redistribution_donors(type_plans, selected)
        if not donors:
            diag["redistribute_reason"] = "no_low_fill_success_donor"
            return
        diag["redistribute_dissolved"] = len(donors)

        pure_pool = [
            repack_ready_item(item)
            for p in selected for item in p.get('packed_items', [])
        ]
        donor_pool = [
            repack_ready_item(item)
            for p in donors for item in p.get('packed_items', [])
        ]
        planned_pure = self._plan_instance_count(pure_pool, target_mpm)
        planned_diss = self._plan_instance_count(
            pure_pool + donor_pool, target_mpm
        )
        # 净增益预判：溶解后可规划达标数须超过"溶解掉的达标 + 纯失败池
        # 本就可规划的达标"，否则拆散达标盘没有意义。
        if planned_diss - len(donors) <= planned_pure:
            diag["redistribute_reason"] = "no_planned_gain"
            return

        t0 = time.time()
        self._consolidate_once(
            type_plans, target_mpm, selected + donors, len(donors),
            t0 + self.REDISTRIBUTE_TIME_BUDGET_S, diag, t0,
            prefix="redistribute", label="指数再分配",
        )

    def _plan_instance_count(
        self, pool: List[Dict], target_mpm: float,
    ) -> int:
        """廉价预判：配方规划器在池上可规划出的达标实例数（不实装）。"""
        if self._sum_mpm(pool) + 1e-9 < float(target_mpm):
            return 0
        from src.main.recipe_planner import plan_recipe_pools
        pools, _meta = plan_recipe_pools(
            pool, float(target_mpm), self.pallet_dims
        )
        return len(pools)

    def _consolidate_once(
        self,
        type_plans: List[Dict],
        target_mpm: float,
        selected: List[Dict],
        baseline_success: int,
        deadline: float,
        diag: Dict,
        t0: float,
        prefix: str = "consolidate",
        label: str = "失败托盘互借修复",
        extract: bool = True,
    ) -> bool:
        """一次合并重装尝试：凑标阶段（可关） + 装满重装 + 棘轮验收。

        baseline_success = 进池托盘中原本已达标的数量（溶解的达标盘）。
        棘轮以它为基线：新达标数必须不低于基线才可能接受。
        诊断键带 prefix（consolidate_* / redistribute_*），两阶段互不覆盖。
        extract=False 用于纯合并阶段（凑标已由 _extract_commit 单独提交）。

        Returns:
            True = 已接受并替换方案；False = 被拒，原方案未动。
        """
        old_fills = [self._fill_rate(p) for p in selected]
        pool = [
            repack_ready_item(item)
            for p in selected
            for item in p.get('packed_items', [])
        ]
        original_ids = {item.get('id') for item in pool}
        if len(original_ids) != len(pool):
            diag[f"{prefix}_reason"] = "duplicate_ids_in_selected"
            return False

        # 已不可再压缩且指数凑不出比基线更多的达标 → 直接跳过（省时）
        vol_lb = self._min_pallets_lower_bound(pool)
        pool_mpm = self._sum_mpm(pool)
        if (len(selected) <= vol_lb
                and pool_mpm + 1e-9 < (baseline_success + 1) * target_mpm):
            diag[f"{prefix}_reason"] = "already_compact_no_success_potential"
            return False

        diag[f"{prefix}_tried"] = 1
        diag[f"{prefix}_old_pallets"] = len(selected)
        diag[f"{prefix}_old_avg_fill"] = (
            sum(old_fills) / len(old_fills) if old_fills else 0.0
        )

        # 凑标阶段：池上重跑配方规划，把凑得出目标指数的达标盘先提出来
        if extract:
            target_sets, rest_pool = self._extract_target_sets(
                pool, target_mpm, deadline
            )
        else:
            target_sets, rest_pool = [], pool
        # 指数再分配的存在前提 = 配方重规划能"实装"出比溶解数更多的达标盘；
        # 实装数不超过基线就立即放弃（省掉注定被棘轮拒绝的装满验证，
        # 剩余池 beam 极少能补出配方都凑不出的达标盘）。
        if baseline_success > 0 and len(target_sets) <= baseline_success:
            diag[f"{prefix}_reason"] = "extraction_below_baseline"
            return False
        rebuilt_rest = self._repack_pool(
            rest_pool, target_mpm,
            expected_count=len(selected),
            prior_success=len(target_sets),
            baseline_success=baseline_success,
            deadline=deadline,
        )
        if rebuilt_rest is None:
            diag[f"{prefix}_reason"] = "aborted_no_improvement_possible"
            return False
        rebuilt_sets = target_sets + rebuilt_rest
        rebuilt_ids = {
            item.get('id') for packed in rebuilt_sets for item in packed
        }
        if rebuilt_ids != original_ids:
            diag[f"{prefix}_reason"] = "box_conservation_failed"
            return False

        new_success = sum(
            1 for packed in rebuilt_sets
            if self._sum_mpm(packed) + 1e-9 >= target_mpm
        )
        diag[f"{prefix}_new_pallets"] = len(rebuilt_sets)
        diag[f"{prefix}_new_success"] = new_success

        # 棘轮（基线 = 溶解进池的达标盘数）：要么盘数严格变少且达标数不低于
        # 基线；要么达标数严格超过基线（达标优先级最高）且失败盘数不增加
        # （新失败盘 ≤ 原失败盘数），防止"+1 达标"以摊出更多低填充碎盘、
        # 或以牺牲溶解的达标盘为代价。
        new_failed = len(rebuilt_sets) - new_success
        old_failed_cnt = len(selected) - baseline_success
        better = (
            (len(rebuilt_sets) < len(selected)
             and new_success >= baseline_success)
            or (new_success > baseline_success
                and new_failed <= old_failed_cnt)
        )
        if not better:
            diag[f"{prefix}_reason"] = "not_better"
            return False

        candidate_plans, new_plans = self._build_candidate_plans(
            type_plans, selected, rebuilt_sets, target_mpm
        )
        # 门禁只验新盘：保留盘未被触碰、已过流水线门禁，避免历史盘问题连坐。
        # target 传入 → 达标盘免 gap（与流水线门禁同源），物理约束恒查。
        for solution in new_plans:
            gate = validate_pallet_constraints(
                solution, self.pallet_dims, constraint_config=self._cfg,
                target_mpm=target_mpm,
            )
            if not gate["is_valid"]:
                diag[f"{prefix}_reason"] = "constraint_gate_failed"
                return False

        type_plans.clear()
        type_plans.extend(candidate_plans)
        new_fills = [
            self._fill_rate_of_items(packed) for packed in rebuilt_sets
        ]
        diag[f"{prefix}_accepted"] = 1
        diag[f"{prefix}_new_avg_fill"] = (
            sum(new_fills) / len(new_fills) if new_fills else 0.0
        )
        net_new_success = max(0, new_success - baseline_success)
        diag["rescued"] += net_new_success
        diag[f"{prefix}_reason"] = "ok"
        dissolved_note = (
            f"（含溶解低填充达标盘 {baseline_success}）"
            if baseline_success else ""
        )
        print(
            f"  - {label}：{len(selected)} 盘{dissolved_note}"
            f"(平均填充 {diag[f'{prefix}_old_avg_fill']:.0%}) 合并重装为 "
            f"{len(rebuilt_sets)} 盘(平均填充 "
            f"{diag[f'{prefix}_new_avg_fill']:.0%})，新增达标 "
            f"{net_new_success}，耗时 {time.time() - t0:.1f}s。"
        )
        return True

    # ------------------------------------------------------------------
    # 池选择与重装
    # ------------------------------------------------------------------

    def _select_pool_pallets(self, failed: List[Dict]) -> List[Dict]:
        """选择进池的失败盘：未够满者全进；超预算时优先最空的。"""
        candidates = [
            p for p in failed
            if self._fill_rate(p) < self.FILL_KEEP_THRESHOLD
        ]
        candidates.sort(key=lambda p: (self._fill_rate(p), str(p.get('pallet_id'))))
        selected: List[Dict] = []
        box_budget = self.MAX_POOL_BOXES
        for p in candidates:
            n = len(p.get('packed_items', []))
            if selected and n > box_budget:
                break
            selected.append(p)
            box_budget -= n
        return selected

    def _select_redistribution_donors(
        self, type_plans: List[Dict], selected_failed: List[Dict],
    ) -> List[Dict]:
        """指数再分配：挑低填充达标盘"溶解"进池参与重规划。

        填充率 70% 就达标的盘，说明装的是小体积高指数箱（指数密度高），
        这是初装指数分配错配的信号。把它们拆开和失败盘箱子一起重跑配方
        规划，高密度箱摊到失败盘的富余体积里 → 同样的总指数凑出更多达标
        盘。溶解风险由棘轮兜底：重装达标数 < 溶解数则整体拒绝、原样保留。

        约束：填充率 < REDISTRIBUTE_FILL_MAX；最空者优先；数量 ≤
        min(REDISTRIBUTE_MAX_DONORS, 失败进池盘数)；池箱数预算不超。
        """
        if not selected_failed:
            return []
        box_budget = self.MAX_POOL_BOXES - sum(
            len(p.get('packed_items', [])) for p in selected_failed
        )
        cap = min(self.REDISTRIBUTE_MAX_DONORS, len(selected_failed))
        candidates = [
            p for p in type_plans
            if p.get('mpm_status') == 'SUCCESS' and p.get('packed_items')
            and self._fill_rate(p) < self.REDISTRIBUTE_FILL_MAX
        ]
        candidates.sort(
            key=lambda p: (self._fill_rate(p), str(p.get('pallet_id')))
        )
        donors: List[Dict] = []
        for p in candidates:
            if len(donors) >= cap:
                break
            n = len(p.get('packed_items', []))
            if n > box_budget:
                continue
            donors.append(p)
            box_budget -= n
        return donors

    #: 每次开盘时喂给装箱器的候选箱窗口（大箱优先；控制单盘搜索成本）
    PACK_CANDIDATE_WINDOW = 240
    #: 整层格栅短路阈值：整层候选填充率达到该值时跳过昂贵的 beam 候选
    LAYERED_SHORTCUT_FILL = 0.85

    def _extract_target_sets(
        self, pool: List[Dict], target_mpm: float, deadline: float,
        remap=None,
    ):
        """凑标阶段：失败池上重跑配方规划，把凑得出目标指数的达标盘先提出来。

        复用初装的配方规划器（整层+顶带枚举、库存约束下最大化达标实例），
        每个实例用整层格栅实装（达标校验；门禁由调用方对全部新盘统一执行）。
        实装失败的实例箱子自然回落剩余池，由装满阶段处理。

        Args:
            remap: 可选 (inst, remaining) -> inst，实装前替换实例箱子
                （凑标局部提交用它把箱子聚拢到尽量少的源托盘）。

        Returns:
            (target_sets, remaining)：达标盘列表 + 剩余箱池。
        """
        sets: List[List[Dict]] = []
        remaining = list(pool)
        if self._sum_mpm(remaining) + 1e-9 < float(target_mpm):
            return sets, remaining
        # 惰性导入，避免 rescue <-> main 模块环
        from src.main.pallet_packer import PalletPacker
        from src.main.recipe_first import _pack_instance
        from src.main.recipe_planner import plan_recipe_pools

        pools, _meta = plan_recipe_pools(
            remaining, float(target_mpm), self.pallet_dims
        )
        if not pools:
            return sets, remaining
        com_fn = self._validate_com
        if com_fn is None:
            from src.geometry.center_of_mass import (
                validate_center_of_mass as com_fn,
            )
        packer = PalletPacker(
            custom_packer_cls=self._CustomPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            build_centered_single_box_solution=(
                build_centered_single_box_solution
            ),
            validate_center_of_mass=com_fn,
            constraint_config=self._cfg,
        )
        for idx, inst in enumerate(pools, 1):
            if time.time() > deadline:
                break
            if remap is not None:
                inst = remap(inst, remaining)
            packed = _pack_instance(
                packer, inst, float(target_mpm), self.pallet_dims, idx,
            )
            if not packed:
                continue
            used = {item.get('id') for item in packed}
            sets.append(packed)
            remaining = [b for b in remaining if b.get('id') not in used]
        return sets, remaining

    def _repack_pool(
        self, pool: List[Dict], target_mpm: float,
        compact_tail: bool = True,
        expected_count: Optional[int] = None,
        prior_success: int = 0,
        baseline_success: int = 0,
        deadline: Optional[float] = None,
    ) -> Optional[List[List[Dict]]]:
        """逐盘重装箱池：每盘用多层 beam 装到装不下为止，直至池空。

        小池（≤60 箱）先走 PoolCompactor 快路径；大池按体积降序取窗口逐盘
        beam；结束后对低填充尾盘做一次二次压实（compact_tail）。任何一轮
        装不进任何箱子时返回已装部分（调用方守恒校验会拒绝接受，原方案
        保持不变），不会死循环。

        超墙钟预算（deadline）不再整体放弃：降级为快速路径（整层格栅 +
        单箱兜底，不跑 beam）收尾，保住此前已产出的达标盘——慢机器上
        只损失装满质量，不损失达标数。

        提前放弃（返回 None，调用方按未改进处理、原方案不动）：
        - 达标产出已不可能守住基线（溶解进池的达标盘数），接受注定失败；
        - 无改进可能：达标数恰好等于基线（无新增）、剩余池指数凑不出达标、
          且已装盘数 + 剩余体积下界 ≥ 原盘数（盘数不可能更少）。
        """
        if not pool:
            return []
        if len(pool) <= 60:
            compact = PoolCompactor(
                self.pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                constraint_config=self._cfg,
            ).compact(pool, max_pallets=3)
            if compact["success"]:
                return compact["packed_sets"]

        remaining = sorted(
            pool,
            key=lambda b: (
                -float(b.get('length', 0) or 0) * float(b.get('width', 0) or 0)
                * float(b.get('height', 0) or 0),
                str(b.get('id')),
            ),
        )
        rebuilt: List[List[Dict]] = []
        success_found = prior_success
        fast_only = False
        futile_rounds = 0
        seed = 52201
        while remaining:
            if (deadline is not None and not fast_only
                    and time.time() > deadline):
                fast_only = True  # 预算尽：快速收尾，别丢已凑出的达标盘
            remaining_mpm = self._sum_mpm(remaining)
            if remaining_mpm + 1e-9 < float(target_mpm):
                if success_found < baseline_success:
                    return None  # 达标数守不住基线 → 接受不可能，别烧时间
                if (
                    success_found == baseline_success
                    and expected_count is not None
                    and len(rebuilt) + self._min_pallets_lower_bound(remaining)
                    >= expected_count
                ):
                    return None  # 既凑不出新达标、盘数也不可能更少
            want_target = (not fast_only) and (
                remaining_mpm + 1e-9 >= float(target_mpm)
            )
            packed = self._pack_one_pallet(
                remaining[:self.PACK_CANDIDATE_WINDOW], target_mpm, seed,
                want_target=want_target, fast_only=fast_only,
            )
            if not packed:
                # 居中单箱兜底（重心安全），保证每轮至少推进一箱，守恒不断裂
                packed = self._centered_single_box(remaining[0])
            if not packed:
                break
            # 徒劳中止：降级模式下整层器对杂箱残池常返回零星结果，逐箱
            # 磨完只会碎成大量单箱盘——反正过不了棘轮，连续 3 轮装不进
            # 3 箱以上就立即放弃（省时；调用方按未改进处理，原方案不动）
            if fast_only:
                futile_rounds = futile_rounds + 1 if len(packed) <= 2 else 0
                if futile_rounds >= 3:
                    return None
            if self._sum_mpm(packed) + 1e-9 >= float(target_mpm):
                success_found += 1
            rebuilt.append(packed)
            used = {item.get('id') for item in packed}
            remaining = [b for b in remaining if b.get('id') not in used]
            seed += 17
        if compact_tail and not fast_only and len(rebuilt) >= 3:
            rebuilt = self._compact_tail_sets(rebuilt, target_mpm)
        return rebuilt

    #: 尾盘压实的填充率门槛：低于此值的盘并池二次重装
    TAIL_FILL_THRESHOLD = 0.55

    def _compact_tail_sets(
        self, rebuilt: List[List[Dict]], target_mpm: float
    ) -> List[List[Dict]]:
        """低填充尾盘二次压实：并池重装，严格更少盘才替换（守恒必须成立）。

        贪心逐盘时大箱先行，尾轮的零散箱（混底面、间隙约束难拼）容易摊成
        碎盘；把它们隔离出来单独再装一遍通常能明显减盘。
        """
        pv = self._pallet_volume()
        if pv <= 0:
            return rebuilt
        tail = [s for s in rebuilt
                if self._items_volume(s) / pv < self.TAIL_FILL_THRESHOLD]
        if len(tail) < 2:
            return rebuilt
        keep = [s for s in rebuilt if s not in tail]
        pool2 = [repack_ready_item(dict(i)) for s in tail for i in s]
        expected = {i.get('id') for s in tail for i in s}

        redone: List[List[Dict]] = []
        if len(pool2) <= 60:
            compact = PoolCompactor(
                self.pallet_dims,
                xy_tolerance=2.0,
                z_tolerance=0.0,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                constraint_config=self._cfg,
            ).compact(pool2, max_pallets=len(tail) - 1)
            if compact["success"]:
                redone = compact["packed_sets"]
        if not redone:
            redone = self._repack_pool(
                pool2, target_mpm, compact_tail=False
            ) or []
        got = {i.get('id') for s in redone for i in s}
        if redone and got == expected and len(redone) < len(tail):
            return keep + redone
        return rebuilt

    def _pack_one_pallet(
        self, window: List[Dict], target_mpm: float, seed: int,
        want_target: bool = False, fast_only: bool = False,
    ) -> List[Dict]:
        """单盘装满：整层格栅 / 纯装满 beam / 凑指数 beam 候选择优。

        - 整层格栅（prefer_fill）：同底面整层 + 顶带，混池分层堆叠填充率最高；
          填充率达 LAYERED_SHORTCUT_FILL 时直接短路（省掉昂贵的 beam）；
        - beam(target=None)：纯装满评分，摆放均衡，能通过收尾的重心清理；
        - beam(target=目标)：偏指数集中（仅 want_target 时跑），偶尔能凑出
          新达标盘（布局偏斜时会被 packer 自身的重心清理清空，空结果落选）。

        fast_only（预算耗尽降级）：只跑整层格栅，空结果交调用方单箱兜底；
        每盘亚秒级，保证降级收尾快速终止。

        选择：want_target（剩余池指数仍够一个达标盘）时优先取达标候选中
        装入体积最大者——达标优先级高于装满；否则纯装入体积最大。
        """
        pv = self._pallet_volume()
        candidates: List[List[Dict]] = []
        layered = build_direct_layer_packing_solution(
            list(window),
            target_mpm=float(target_mpm),
            pallet_dims=self.pallet_dims,
            seed=seed,
            xy_tolerance=2.0,
            z_tolerance=0.0,
            candidate_count=12,
            prefer_fill=True,
            constraint_config=self._cfg,
        )
        if fast_only:
            return layered or []
        if layered:
            candidates.append(layered)
            # 整层短路：已足够密、且（无凑标需求或已达标）→ 不再跑 beam
            layered_dense = (
                pv > 0
                and self._items_volume(layered) / pv
                >= self.LAYERED_SHORTCUT_FILL
            )
            layered_meets = (
                self._sum_mpm(layered) + 1e-9 >= float(target_mpm)
            )
            if layered_dense and (not want_target or layered_meets):
                return layered
        # 同底面同质候选：单一箱型的紧密网格天然满足 6mm 间隙约束，
        # 混底面窗口装不出时（跨类间隙连锁违规）这是最稳的高填充来源。
        by_fp: Dict = {}
        for b in window:
            key = tuple(sorted((
                round(float(b.get('length', 0) or 0)),
                round(float(b.get('width', 0) or 0)),
            )))
            by_fp.setdefault(key, []).append(b)
        if len(by_fp) > 1:
            top_group = max(by_fp.values(), key=len)
            if len(top_group) >= 4:
                # 整层器语义是"奔着 target 停"（不可达返回空），所以给它
                # 一个按几何算出的可达 target：单盘最多能放的箱数 × 组内最小
                # 指数。target 精确可达 → 装出满层高填充盘。
                homo_target = self._homogeneous_reachable_target(top_group)
                if homo_target > 0:
                    homo = build_direct_layer_packing_solution(
                        list(top_group),
                        target_mpm=homo_target,
                        pallet_dims=self.pallet_dims,
                        seed=seed,
                        xy_tolerance=2.0,
                        z_tolerance=0.0,
                        candidate_count=12,
                        prefer_fill=True,
                        constraint_config=self._cfg,
                    )
                    if homo:
                        candidates.append(homo)
        beam_targets = (None, float(target_mpm)) if want_target else (None,)
        for tgt in beam_targets:
            packer = self._CustomPacker(
                self.pallet_dims,
                support_ratio_threshold=self._cfg.support_ratio_threshold,
                size_tolerance=2.0,
                max_candidate_points=200,
                max_points_per_layer=40,
                constraint_config=self._cfg,
            )
            packed, _ = packer.pack(
                window,
                num_restarts=2,
                beam_width=3,
                candidate_limit=10,
                random_seed=seed,
                target_mpm=tgt,
                stop_when_target_met=False,
                allow_skip_items=True,
            )
            if packed:
                candidates.append(packed)
        if not candidates:
            return []
        if want_target:
            reaching = [
                c for c in candidates
                if self._sum_mpm(c) + 1e-9 >= float(target_mpm)
            ]
            if reaching:
                return max(reaching, key=self._items_volume)
        return max(candidates, key=self._items_volume)

    def _homogeneous_reachable_target(self, group: List[Dict]) -> float:
        """同底面组的单盘可达指数：网格估算单盘最大箱数 × 组内最小指数。

        网格数取两种朝向的较大者（保守下估，整层器可自行超出），层数按
        托盘高整除箱高。返回 0 表示算不出（尺寸缺失等），调用方跳过。
        """
        pl = float(self.pallet_dims.get('length', 0) or 0)
        pw = float(self.pallet_dims.get('width', 0) or 0)
        ph = float(self.pallet_dims.get('height', 0) or 0)
        b0 = group[0]
        bl = float(b0.get('length', 0) or 0) + 2.0
        bw = float(b0.get('width', 0) or 0) + 2.0
        heights = [float(b.get('height', 0) or 0) for b in group]
        bh = min(h for h in heights if h > 0) if any(heights) else 0.0
        if min(pl, pw, ph, bl, bw, bh) <= 0:
            return 0.0
        per_layer = max(
            int(pl // bl) * int(pw // bw),
            int(pl // bw) * int(pw // bl),
        )
        layers = int(ph // bh)
        if per_layer <= 0 or layers <= 0:
            return 0.0
        n_cap = min(len(group), per_layer * layers)
        mpms = [float(b.get('min_pack_multiple', 0) or 0) for b in group]
        min_mpm = min(mpms) if mpms else 0.0
        if n_cap <= 0 or min_mpm <= 0:
            return 0.0
        return n_cap * min_mpm

    def _items_volume(self, items: List[Dict]) -> float:
        return sum(
            float(i.get('length', 0) or 0)
            * float(i.get('width', 0) or 0)
            * float(i.get('height', 0) or 0)
            for i in items
        )

    def _centered_single_box(self, box: Dict) -> List[Dict]:
        """单箱居中兜底：复用生产原语（仅受边界/重心/吸盘约束，天然安全）。"""
        return build_centered_single_box_solution(
            [box],
            self.pallet_dims,
            xy_tolerance=2.0,
            z_tolerance=0.0,
            support_ratio_threshold=self._cfg.support_ratio_threshold,
            constraint_config=self._cfg,
        )

    def _build_candidate_plans(
        self,
        type_plans: List[Dict],
        selected: List[Dict],
        rebuilt_sets: List[List[Dict]],
        target_mpm: float,
    ):
        """保留未进池托盘，追加重装后的新托盘（字段与其它救援器一致）。

        Returns:
            (candidate_plans, new_plans)：完整候选列表 + 其中新建的托盘。
        """
        selected_ids = {id(p) for p in selected}
        kept = [p for p in type_plans if id(p) not in selected_ids]
        template = selected[0]
        pallet_type = template.get('pallet_type', 'UNKNOWN')
        sales_order_no = template.get('sales_order_no', 'UNKNOWN_ORDER')
        used_ids = {str(p.get('pallet_id')) for p in kept}

        candidate_plans = list(kept)
        new_plans: List[Dict] = []
        idx = 0
        for packed in rebuilt_sets:
            if not packed:
                continue
            idx += 1
            pid = f"{pallet_type}-{sales_order_no}-RC{idx}"
            while pid in used_ids:
                idx += 1
                pid = f"{pallet_type}-{sales_order_no}-RC{idx}"
            used_ids.add(pid)
            solution = {
                "pallet_id": pid,
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_target": target_mpm,
                "rescue_consolidated": True,
            }
            PalletEvaluator.calc_pallet_status(solution)
            solution["stability_checks"] = {}
            if self._validate_com is not None:
                com = self._validate_com(solution, self.pallet_dims)
                if com.get("is_stable", False):
                    solution["stability_checks"]["status"] = "SUCCESS"
                else:
                    solution["stability_checks"]["center_of_mass_failure"] = com
                    solution["stability_checks"]["status"] = "FAILED"
            candidate_plans.append(solution)
            new_plans.append(solution)
        return candidate_plans, new_plans

    # ------------------------------------------------------------------
    # 度量辅助
    # ------------------------------------------------------------------

    def _pallet_volume(self) -> float:
        return (
            float(self.pallet_dims.get('length', 0) or 0)
            * float(self.pallet_dims.get('width', 0) or 0)
            * float(self.pallet_dims.get('height', 0) or 0)
        )

    def _fill_rate(self, plan: Dict) -> float:
        return self._fill_rate_of_items(plan.get('packed_items', []))

    def _fill_rate_of_items(self, items: List[Dict]) -> float:
        pallet_volume = self._pallet_volume()
        if pallet_volume <= 0:
            return 0.0
        box_volume = sum(
            float(i.get('length', 0) or 0)
            * float(i.get('width', 0) or 0)
            * float(i.get('height', 0) or 0)
            for i in items
        )
        return box_volume / pallet_volume

    def _min_pallets_lower_bound(self, pool: List[Dict]) -> int:
        """体积下界：池内箱子无论怎么装至少需要的托盘数。"""
        pallet_volume = self._pallet_volume()
        if pallet_volume <= 0:
            return 1
        total = sum(
            float(b.get('length', 0) or 0)
            * float(b.get('width', 0) or 0)
            * float(b.get('height', 0) or 0)
            for b in pool
        )
        return max(1, int(-(-total // pallet_volume)))

    def _sum_mpm(self, items: List[Dict]) -> float:
        """计算物品列表中所有箱子的 MPM 总和。"""
        return sum(
            float(x.get('min_pack_multiple', 0) or 0)
            for x in items
        )
