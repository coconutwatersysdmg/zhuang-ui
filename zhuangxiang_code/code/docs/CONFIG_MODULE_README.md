# 配置模块使用指南

> **更新提示（2026-06）**：**装箱约束**（间隙、支撑率、重心、吸盘几何、各约束开关、
> 箱子旋转 `allow_box_rotation_90`）与主算法开关 `main_packer` 已统一迁移到
> `ConstraintConfig`（`src/config/constraint_config.py`）+ YAML `constraints` 段，
> 配置与扩展见 [`约束配置与扩展指南.md`](约束配置与扩展指南.md)。本文档描述的
> `PalletConfig` / `PackingAlgorithmConfig` / `RobotSuctionConfig` / `RescueConfig`
> 仍有效（托盘/算法/吸盘/救援参数），但下文部分示例的托盘尺寸（1200×1000×1450）
> 是早期默认；当前 MH423C 实际为 **1440×2240×720**（见 `config/packing_config.yaml`）。

## 概述

配置模块提供了装箱系统所有可配置参数的统一管理接口，支持从YAML文件或API字典加载配置。

## 目录结构

```
src/config/
├── __init__.py              # 模块导出
├── constants.py             # 全局常量
├── pallet_config.py         # 托盘配置类
├── algorithm_config.py      # 算法参数配置类
└── loader.py                # 配置加载器
```

## 快速开始

### 1. 导入配置模块

```python
from src.config import (
    PALLET_INDEX_TARGETS,
    MAX_BOX_GAP_MM,
    PalletConfig,
    PackingAlgorithmConfig,
    RobotSuctionConfig,
    RescueConfig,
)
from src.config.loader import ConfigLoader
```

### 2. 使用全局常量

```python
# 托盘指数目标
print(PALLET_INDEX_TARGETS)  # {'MH423C': 192, 'MH110': 32}

# 箱间最大间隙
print(MAX_BOX_GAP_MM)  # 6.0
```

### 3. 从YAML文件加载配置

```python
from pathlib import Path

config_path = Path("config.yaml")
loader = ConfigLoader(config_path)

# 加载托盘配置
pallet_config = loader.load_pallet_config("MH423C")
print(pallet_config.length)  # 1200.0

# 加载算法配置
algo_config = loader.load_algorithm_config()
print(algo_config.num_restarts)  # 30
```

### 4. 从字典加载配置（API接口）

```python
# 前端传入的配置（只需传入需要修改的参数）
frontend_config = {
    'algorithm': {
        'num_restarts': 50,
        'beam_width': 10,
    },
    'robot': {
        'cup_length': 700.0,
    }
}

loader = ConfigLoader.from_dict(frontend_config)
algo_config = loader.load_algorithm_config()

print(algo_config.num_restarts)  # 50 (前端传入)
print(algo_config.support_ratio_threshold)  # 0.8 (使用默认值)
```

### 5. 使用默认配置

```python
loader = ConfigLoader.default()
algo_config = loader.load_algorithm_config()
# 所有参数使用默认值
```

## 配置类详解

### PalletConfig - 托盘配置

```python
@dataclass(frozen=True)
class PalletConfig:
    pallet_type: str        # 托盘类型代码
    length: float           # 托盘长度（毫米）
    width: float            # 托盘宽度（毫米）
    height: float           # 托盘高度（毫米）
    target_index: float     # 目标最小包装量倍数和
```

**方法**:
- `to_dims_dict()`: 转换为尺寸字典格式（兼容原代码）
- `from_dict(data)`: 从字典创建配置对象

### PackingAlgorithmConfig - 装箱算法配置

```python
@dataclass(frozen=True)
class PackingAlgorithmConfig:
    support_ratio_threshold: float = 0.8    # 支撑面积比例阈值
    xy_tolerance: float = 2.0               # XY方向尺寸容差（毫米）
    z_tolerance: float = 0.0                # Z方向尺寸容差（毫米）
    num_restarts: int = 30                  # 多起点搜索次数
    beam_width: int = 6                     # Beam Search 宽度
    candidate_limit: int = 30               # 每个状态保留的候选放置数量上限
    max_candidate_points: int = 200         # 最大候选点数量
    max_points_per_layer: int = 40          # 每层最大候选点数量
```

### RobotSuctionConfig - 机器人吸盘配置

```python
@dataclass(frozen=True)
class RobotSuctionConfig:
    cup_length: float = 600.0               # 吸盘长度（毫米）
    cup_width: float = 800.0                # 吸盘宽度（毫米）
    xy_clearance: float = 0.0               # XY方向安全间隙（毫米）
    z_clearance: float = 0.0                # Z方向安全间隙（毫米）
    allow_rotation_90: bool = True          # 是否允许吸盘旋转90度
    reachability_enabled: bool = True       # 是否启用机器人可达性检查
```

### RescueConfig - 失败托盘救援配置

```python
@dataclass(frozen=True)
class RescueConfig:
    max_gap: float = 64.0                   # 最大可救援的指数缺口
    max_attempts: int = 80                  # 最大救援尝试次数
    topup_max_donor_scan: int = 80          # 补齐策略最大扫描捐赠箱子数
    topup_donor_mpm_slack: float = 16.0     # 补齐策略捐赠者指数松弛量
    hole_fill_max_donor_scan: int = 160     # 补洞策略最大扫描捐赠箱子数
    hole_fill_max_add_items: int = 8        # 补洞策略最大添加箱子数
    recipe_max_group_boxes: int = 400       # 配方重建最大箱子组大小
    recipe_max_count: int = 12              # 配方重建最大配方数量
    seed_base_topup: int = 31000            # 补齐策略随机种子基数
    seed_base_hole_fill: int = 41000        # 补洞策略随机种子基数
    seed_base_recipe: int = 51000           # 配方重建随机种子基数
```

## YAML配置文件格式

```yaml
pallets:
  MH423C:
    length: 1200.0
    width: 1000.0
    height: 1450.0
    target_index: 192
  MH110:
    length: 1200.0
    width: 800.0
    height: 1450.0
    target_index: 32

algorithm:
  support_ratio_threshold: 0.8
  xy_tolerance: 2.0
  z_tolerance: 0.0
  num_restarts: 30
  beam_width: 6
  candidate_limit: 30
  max_candidate_points: 200
  max_points_per_layer: 40

robot:
  cup_length: 600.0
  cup_width: 800.0
  xy_clearance: 0.0
  z_clearance: 0.0
  allow_rotation_90: true
  reachability_enabled: true

rescue:
  max_gap: 64.0
  max_attempts: 80
  topup_max_donor_scan: 80
  topup_donor_mpm_slack: 16.0
  hole_fill_max_donor_scan: 160
  hole_fill_max_add_items: 8
  recipe_max_group_boxes: 400
  recipe_max_count: 12
```

## API接口示例

### 完整工作流

```python
from src.config.loader import ConfigLoader

# 1. 前端传入配置
frontend_config = {
    'algorithm': {
        'num_restarts': 40,
        'beam_width': 8,
        'support_ratio_threshold': 0.85
    },
    'robot': {
        'cup_length': 650.0,
        'allow_rotation_90': True
    },
    'rescue': {
        'max_gap': 70.0,
        'max_attempts': 100
    }
}

# 2. 加载配置
loader = ConfigLoader.from_dict(frontend_config)
algo_config = loader.load_algorithm_config()
robot_config = loader.load_robot_config()
rescue_config = loader.load_rescue_config()

# 3. 使用配置
print(f"算法重启次数: {algo_config.num_restarts}")  # 40
print(f"吸盘长度: {robot_config.cup_length}")  # 650.0
print(f"最大缺口: {rescue_config.max_gap}")  # 70.0
```

## 设计特性

### 1. 不可变配置
所有配置类使用 `@dataclass(frozen=True)`，确保配置对象创建后不可修改。

```python
config = PackingAlgorithmConfig()
config.num_restarts = 50  # ❌ 错误：配置对象不可修改
```

### 2. 默认值支持
所有配置类都提供合理的默认值，前端只需传入需要修改的参数。

```python
# 只修改部分参数，其他使用默认值
config = PackingAlgorithmConfig.from_dict({
    'num_restarts': 50
})
print(config.beam_width)  # 6 (默认值)
```

### 3. 类型安全
使用类型提示，IDE可以提供自动补全和类型检查。

```python
config: PackingAlgorithmConfig = loader.load_algorithm_config()
# IDE会提示所有可用属性
```

### 4. 向后兼容
配置类提供转换方法，兼容原代码的字典格式。

```python
pallet_config = loader.load_pallet_config("MH423C")
pallet_dims = pallet_config.to_dims_dict()
# {'length': 1200.0, 'width': 1000.0, 'height': 1450.0}
```

## 测试

运行测试套件：

```bash
python test_config.py
```

运行使用示例：

```bash
python example_config_usage.py
```

## 常见问题

### Q1: 如何添加新的配置参数？

1. 在对应的配置类中添加新字段
2. 在 `config.yaml` 中添加默认值
3. 更新测试用例

### Q2: 前端需要传入所有参数吗？

不需要。前端只需传入需要修改的参数，其他参数会使用默认值。

### Q3: 配置对象可以修改吗？

不可以。所有配置对象都是不可变的（frozen=True），确保配置安全。

### Q4: 如何生成默认配置文件？

```python
from src.config.loader import create_default_config_yaml
from pathlib import Path

create_default_config_yaml(Path("config.yaml"))
```
