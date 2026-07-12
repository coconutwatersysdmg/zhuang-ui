"""已知最优订单构造器。

核心洞察：构造「总指数 = 192×N、0 残料」且每盘存在一种合法摆法达标的订单，
则理论上界 = 最优 = N 个达标盘。跑算法得实际达标 S，S/N 即「离最优多远」，
绕开贪心外推的选择偏差（上界是构造已知，不是估计）。

几何口径与真实算法对齐：
- XY 间隙容差 2mm（与 global_column_packer._orient_per、recipe_first._oriented_for_pallet
  同口径）；Z 容差 0（与输出 schema 一致）。
- 每盘箱数按「允许 90° 旋转的最优朝向」算，故 N 是允许旋转时的真上界；
  对「固定朝向装不满」的旋转敏感原型，固定朝向 < N、旋转后 = N，正是审计点。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PALLET: Dict[str, float] = {'length': 1440.0, 'width': 2240.0, 'height': 720.0}
PALLET_TYPE = 'MH423C'
TARGET = 192.0
XY_TOL = 2.0  # 与 _orient_per / _oriented_for_pallet 同口径


def _per_layer_fixed(length: float, width: float,
                     pallet: Dict[str, float], tol: float = XY_TOL) -> int:
    """固定朝向（length 沿托盘长、width 沿托盘宽）每层格数。"""
    return int(pallet['length'] // (length + tol)) * int(
        pallet['width'] // (width + tol))


def _per_layer_best(length: float, width: float,
                    pallet: Dict[str, float], tol: float = XY_TOL) -> int:
    """两朝向较优的每层格数（允许 90° 旋转）。"""
    a = _per_layer_fixed(length, width, pallet, tol)
    b = _per_layer_fixed(width, length, pallet, tol)
    return max(a, b)


def _layers(height: float, pallet: Dict[str, float]) -> int:
    """可叠层数（Z 容差 0）。"""
    return int(pallet['height'] // height)


@dataclass(frozen=True)
class Archetype:
    """一种「同构盘原型」：单一箱型填满整盘，已知达标摆法。

    Attributes:
        label: 短标签（用于箱 id / type）。
        length/width/height: 箱尺寸（mm，按给定朝向喂给算法）。
        mpm: 单箱指数。
        rotation_sensitive: True=固定朝向装不满、须旋转才达标（审计标记）。
        unreachable: True=此原型几何上单盘到不了 target（用于阈值/装满测试，
            其盘数不计入最优 N，仅测「不达标尽量装满」）。
    """

    label: str
    length: float
    width: float
    height: float
    mpm: float
    rotation_sensitive: bool = False
    unreachable: bool = False

    @property
    def per_pallet(self) -> int:
        """允许旋转的每盘箱数（最优朝向）。"""
        return _per_layer_best(self.length, self.width, PALLET) * _layers(
            self.height, PALLET)

    @property
    def per_pallet_fixed(self) -> int:
        """固定朝向的每盘箱数。"""
        return _per_layer_fixed(self.length, self.width, PALLET) * _layers(
            self.height, PALLET)

    @property
    def pallet_mpm_best(self) -> float:
        """满盘指数（最优朝向）。"""
        return self.per_pallet * self.mpm

    @property
    def pallet_mpm_fixed(self) -> float:
        """满盘指数（固定朝向）。"""
        return self.per_pallet_fixed * self.mpm

    def validate(self) -> 'Archetype':
        """校验几何不变量；返回自身便于链式。"""
        assert self.per_pallet >= 1, f'{self.label}: 箱子装不进托盘'
        assert _layers(self.height, PALLET) >= 1, f'{self.label}: 超高'
        if self.unreachable:
            assert self.pallet_mpm_best + 1e-9 < TARGET, (
                f'{self.label}: 标记 unreachable 但满盘 {self.pallet_mpm_best} ≥ {TARGET}')
        else:
            assert abs(self.pallet_mpm_best - TARGET) < 1e-9, (
                f'{self.label}: per_pallet({self.per_pallet})×mpm({self.mpm})'
                f'={self.pallet_mpm_best} ≠ {TARGET}（非 0 残料构造）')
        if self.rotation_sensitive:
            assert self.pallet_mpm_fixed + 1e-9 < TARGET <= self.pallet_mpm_best + 1e-9, (
                f'{self.label}: 标记旋转敏感但固定满盘={self.pallet_mpm_fixed}、'
                f'旋转满盘={self.pallet_mpm_best}，未跨越 target')
        return self


def _boxes_of(arch: Archetype, order_no: str, pallet_seq: int) -> List[Dict]:
    """生成填满一盘 arch 的箱子列表（贴合 run_with_boxes 入参 schema）。"""
    n = arch.per_pallet
    out: List[Dict] = []
    for k in range(n):
        out.append({
            'id': f'{arch.label}-{order_no}-{pallet_seq}-{k}',
            'type': arch.label,
            'length': float(arch.length), 'width': float(arch.width),
            'height': float(arch.height), 'weight': 1.0,
            'min_pack_multiple': float(arch.mpm), 'is_small_box': False,
            'pallet_type': PALLET_TYPE, 'sales_order_no': order_no,
            'pallet_dims': dict(PALLET),
        })
    return out


@dataclass(frozen=True)
class Order:
    """一个构造订单：箱子列表 + 已知最优 N + 元数据。"""

    name: str
    boxes: List[Dict]
    n_optimal: int          # 可达标盘数上界（仅 reachable 原型贡献）
    n_unreachable: int      # 几何不可达标的「装满测试」盘数
    specs: List[Tuple[str, int]]  # [(原型label, 盘数), ...]
    rotation_sensitive: bool

    @property
    def total_mpm(self) -> float:
        return sum(float(b['min_pack_multiple']) for b in self.boxes)


def build_order(name: str, specs: List[Tuple[Archetype, int]],
                order_no: Optional[str] = None) -> Order:
    """把 [(原型, 盘数)] 组装成一个销售订单（同一 order_no → 同一分组）。

    校验：每个原型几何不变量 + 全单总指数 = 192 × (可达标盘数)。
    """
    order_no = order_no or name
    boxes: List[Dict] = []
    n_opt = 0
    n_unreach = 0
    flat_specs: List[Tuple[str, int]] = []
    rot_sensitive = False
    seq = 0
    for arch, count in specs:
        arch.validate()
        flat_specs.append((arch.label, count))
        rot_sensitive = rot_sensitive or arch.rotation_sensitive
        for _ in range(count):
            seq += 1
            boxes.extend(_boxes_of(arch, order_no, seq))
        if arch.unreachable:
            n_unreach += count
        else:
            n_opt += count
    order = Order(
        name=name, boxes=boxes, n_optimal=n_opt, n_unreachable=n_unreach,
        specs=flat_specs, rotation_sensitive=rot_sensitive,
    )
    # 全单守恒不变量：可达标部分总指数严格 = 192 × n_optimal
    reach_mpm = sum(
        float(b['min_pack_multiple']) for b in boxes
        if not _label_unreachable(b['type'], specs)
    )
    assert abs(reach_mpm - TARGET * n_opt) < 1e-6, (
        f'{name}: 可达标总指数 {reach_mpm} ≠ 192×{n_opt}')
    return order


def _label_unreachable(label: str, specs: List[Tuple[Archetype, int]]) -> bool:
    for arch, _ in specs:
        if arch.label == label:
            return arch.unreachable
    return False


# —— 预置原型库（全部 350/265 网格对齐，便于路由 GCP；尺寸取自真实箱域）——

def reg_multilayer(mpm: float = 2.0) -> Archetype:
    """规则多层：350×265×240，每盘 96 箱×mpm2=192（GCP 规范规则盘）。"""
    return Archetype('REGm', 350, 265, 240, mpm).validate()


def reg_wide(mpm: float = 4.0) -> Archetype:
    """规则宽底：350×530×240，每盘 48 箱×mpm4=192。"""
    return Archetype('REGw', 350, 530, 240, mpm).validate()


def reg_big(mpm: float = 8.0) -> Archetype:
    """规则大底：700×530×240，每盘 24 箱×mpm8=192。"""
    return Archetype('REGb', 700, 530, 240, mpm).validate()


def single_layer(mpm: float = 6.0) -> Archetype:
    """纯单层：350×265×480（>360 只 1 层），每盘 32 箱×mpm6=192（C 盲区区）。"""
    return Archetype('SING', 350, 265, 480, mpm).validate()


def big_mpm() -> Archetype:
    """大 mpm 少箱：700×530×720（满高单箱独占柱），每盘 8 箱×mpm24=192。"""
    return Archetype('BIGm', 700, 530, 720, 24).validate()


def rotation_sensitive() -> Archetype:
    """旋转敏感：530×350×240 按「坏朝向」给出。

    固定朝向每层 12（满盘 144<192），旋转后每层 16（满盘 192）。固定朝向装不满，
    必须 90° 旋转才达标——审计 baseline 固定朝向缺陷的核心原型。
    """
    return Archetype('ROT', 530, 350, 240, 4, rotation_sensitive=True).validate()


def build_balanced_order(name: str, n_pallets: int,
                         order_no: Optional[str] = None) -> Order:
    """异质柱指数均衡分配订单（压测柱级 Set-Partitioning ILP）。

    同底面同高 350×265×240，每盘 48 箱 mpm1 + 48 箱 mpm3 = 48+144 = 192。
    凑柱后柱指数异质（3..9）。全单总指数恰 192×N，最优 = 高/低指数柱均匀
    配盘；劣解聚成「超盘(>192 浪费)+欠盘(<192 不达标)」→ S<N。两类同底面同高
    →96 箱必装满一盘，N 是真上界（几何无歧义）。
    """
    order_no = order_no or name
    boxes: List[Dict] = []
    per_half = 48  # 每盘每种 mpm 各 48 箱（96 箱填满一盘）
    for p in range(n_pallets):
        for mpm, lab in ((1.0, 'BALlo'), (3.0, 'BALhi')):
            for k in range(per_half):
                boxes.append({
                    'id': f'{lab}-{order_no}-{p}-{k}', 'type': lab,
                    'length': 350.0, 'width': 265.0, 'height': 240.0,
                    'weight': 1.0, 'min_pack_multiple': mpm,
                    'is_small_box': False, 'pallet_type': PALLET_TYPE,
                    'sales_order_no': order_no, 'pallet_dims': dict(PALLET),
                })
    total = sum(b['min_pack_multiple'] for b in boxes)
    assert abs(total - TARGET * n_pallets) < 1e-6, (
        f'{name}: 总指数 {total} ≠ 192×{n_pallets}')
    return Order(name=name, boxes=boxes, n_optimal=n_pallets, n_unreachable=0,
                 specs=[('BALlo', n_pallets), ('BALhi', n_pallets)],
                 rotation_sensitive=False)


def big_low_unreachable(mpm: float = 4.0) -> Archetype:
    """大底低指数、单盘几何不可达标：700×530×240×mpm4，满盘 24×4=96<192。

    用于阈值跨越与「不达标尽量装满」测试；其盘不计入最优 N。
    """
    return Archetype('BLOW', 700, 530, 240, mpm, unreachable=True).validate()


def build_mixed_characteristics_order(name: str = 'mega',
                                      order_no: Optional[str] = None) -> Order:
    """一张订单混入全部「特性」箱型，验证算法能否按特性分流并全局优化：

    - 规则 350×265×240（2 盘）           → 应走 GCP
    - 旋转敏感 530×350×240（1 盘，坏朝向） → 应走 GCP（自带旋转）
    - 超高密度 358×558×240（1 盘，~99%）  → 应走 GCP
    - 异质柱 700×265×240 mpm2+mpm6（2 盘） → GCP 弱项（已知盲区）
    - 不可达标杂箱 700×530×240 mpm4（1 盘量，满盘 96<192）→ 应走 baseline、尽量装满

    全在同一 order_no（同组）。可达标盘 N=6（2+1+1+2），外加 1 份不可达标杂箱
    （只测尽量装满，不计入 N）。各特性箱底面互不相同（除规则/异质柱外），便于看分流。
    """
    order_no = order_no or name
    boxes: List[Dict] = []
    specs: List[Tuple[str, int]] = []
    seq = 0
    for arch, npal, tag in ((reg_multilayer(), 2, 'regular350x265'),
                            (rotation_sensitive(), 1, 'rot530x350'),
                            (dense_grid_a(), 1, 'dense358x558')):
        arch.validate()
        for _ in range(npal):
            seq += 1
            boxes.extend(_boxes_of(arch, order_no, seq))
        specs.append((tag, npal))
    # 异质柱 700×265×240：每盘 24×mpm2 + 24×mpm6 = 192（同尺寸异 mpm）
    for _p in range(2):
        seq += 1
        for mpm, cnt in ((2.0, 24), (6.0, 24)):
            for k in range(cnt):
                boxes.append({
                    'id': f'HET-{order_no}-{seq}-{int(mpm)}-{k}', 'type': 'HET700x265',
                    'length': 700.0, 'width': 265.0, 'height': 240.0, 'weight': 1.0,
                    'min_pack_multiple': mpm, 'is_small_box': False,
                    'pallet_type': PALLET_TYPE, 'sales_order_no': order_no,
                    'pallet_dims': dict(PALLET),
                })
    specs.append(('het700x265_mpm2+6', 2))
    n_opt = 2 + 1 + 1 + 2
    # 不可达标杂箱 700×530×240 mpm4（满盘 96<192）
    unreach = big_low_unreachable()
    seq += 1
    boxes.extend(_boxes_of(unreach, order_no, seq))
    specs.append(('unreachable700x530', 1))
    return Order(name=name, boxes=boxes, n_optimal=n_opt, n_unreachable=1,
                 specs=specs, rotation_sensitive=True)


# 无异质柱的高底面多样性混合订单：每个底面单一箱型（同尺寸同 mpm，0% het），
# 各底面整数盘 → N=各底面盘数之和（真上界）。15 种底面 > GCP 柱类型上限 14，
# 联合 GCP 会退贪心 → 用于测「拆细成子订单能否恢复最优」。
_MIXED_NOHET_SPECS = [
    # (长, 宽, 高, mpm, 盘数)；per_pallet×mpm 必 =192
    (350, 265, 240, 2, 4), (178, 558, 240, 2, 2), (350, 530, 240, 4, 3),
    (358, 558, 240, 4, 3), (358, 530, 240, 4, 2), (700, 530, 240, 8, 3),
    (718, 558, 240, 8, 3), (178, 265, 240, 1, 1), (700, 558, 240, 8, 3),
    (718, 530, 240, 8, 3), (718, 265, 240, 4, 2), (358, 265, 240, 2, 2),
    (350, 558, 240, 4, 2), (700, 265, 240, 4, 2), (530, 558, 240, 8, 2),
]


def build_mixed_nohet_order(name: str = 'mixed2000',
                            order_no: Optional[str] = None) -> Order:
    """≈2000 箱、15 种底面、无异质柱的混合订单（每底面单一箱型）。

    每底面整数盘、各自可达标 → N=盘数之和（真上界）。底面种类(15)>GCP 柱类型
    上限(14)→联合 GCP 退贪心，用于检验「把高多样性订单拆成更细子订单（各底面
    种类少、各自走精确 ILP）能否得到更高达标率」。
    """
    order_no = order_no or name
    boxes: List[Dict] = []
    specs: List[Tuple[str, int]] = []
    n_opt = 0
    seq = 0
    for i, (L, W, H, mpm, npal) in enumerate(_MIXED_NOHET_SPECS):
        arch = Archetype(f'F{i:02d}', L, W, H, mpm).validate()
        for _ in range(npal):
            seq += 1
            boxes.extend(_boxes_of(arch, order_no, seq))
        specs.append((arch.label, npal))
        n_opt += npal
    return Order(name=name, boxes=boxes, n_optimal=n_opt, n_unreachable=0,
                 specs=specs, rotation_sensitive=False)


# —— 超高密度原型（体积填充 ≈98~99%，压测 CP-SAT 紧贴落地 / 爆盘回退）——
# 底面贴着「+2mm 容差后整除托盘」选，几乎零余量；满高 3×240=720。

def dense_grid_a() -> Archetype:
    """密栅 A：358×558×240（4×4 格恰填 1440×2240），每盘 48 箱×mpm4=192，填充≈99%。"""
    return Archetype('DNa', 358, 558, 240, 4).validate()


def dense_grid_b() -> Archetype:
    """密栅 B：718×558×240（2×4 格），每盘 24 箱×mpm8=192，填充≈99.4%。"""
    return Archetype('DNb', 718, 558, 240, 8).validate()


def dense_grid_c() -> Archetype:
    """密栅 C：178×558×240（8×4 格），每盘 96 箱×mpm2=192，填充≈98.5%。"""
    return Archetype('DNc', 178, 558, 240, 2).validate()


def pallet_fill_rate(arch: Archetype) -> float:
    """该原型满盘体积填充率（理论）。"""
    box_vol = arch.length * arch.width * arch.height
    pallet_vol = PALLET['length'] * PALLET['width'] * PALLET['height']
    return arch.per_pallet * box_vol / pallet_vol


def build_dense_mixed_order(name: str, n_pallets: int,
                            order_no: Optional[str] = None) -> Order:
    """超高密度混底面订单（压测 CP-SAT 2D 摆放 + 爆盘回退）。

    每盘 = 24 箱 P(358×558×240,mpm4) + 12 箱 Q(718×558×240,mpm8) = 96+96 = 192。
    两底面共 558 宽、同享 560 高的行带（2 行 P + 2 行 Q，每层 8P+4Q，×3 层）；
    填充≈99%。CP-SAT 须把 358 宽与 718 宽两种柱排进 1440×2240 几乎无余量的底面，
    比单一密栅更难。可证最优：每盘恰 24P+12Q 摆得下且达 192，N 是真上界。
    """
    order_no = order_no or name
    p_arch = Archetype('DMp', 358, 558, 240, 4)
    q_arch = Archetype('DMq', 718, 558, 240, 8)
    boxes: List[Dict] = []
    for p in range(n_pallets):
        for arch, cnt in ((p_arch, 24), (q_arch, 12)):
            for k in range(cnt):
                boxes.append({
                    'id': f'{arch.label}-{order_no}-{p}-{k}', 'type': arch.label,
                    'length': float(arch.length), 'width': float(arch.width),
                    'height': float(arch.height), 'weight': 1.0,
                    'min_pack_multiple': float(arch.mpm), 'is_small_box': False,
                    'pallet_type': PALLET_TYPE, 'sales_order_no': order_no,
                    'pallet_dims': dict(PALLET),
                })
    total = sum(b['min_pack_multiple'] for b in boxes)
    assert abs(total - TARGET * n_pallets) < 1e-6, (
        f'{name}: 总指数 {total} ≠ 192×{n_pallets}')
    return Order(name=name, boxes=boxes, n_optimal=n_pallets, n_unreachable=0,
                 specs=[('DMp', n_pallets), ('DMq', n_pallets)],
                 rotation_sensitive=False)
