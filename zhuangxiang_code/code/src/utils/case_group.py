"""case_group 同组约束的共享原语（单一事实来源）。

业务规则：箱子可带 ``case_group`` 属性——
- 值为 0（或缺失/空/None/NaN）＝ 无约束；
- 值非 0 ＝ 该箱只能与 **相同 case_group 值** 的箱子拼到同一个托盘上。

推论：一个合法托盘上所有箱子的（归一化）case_group 必须完全相同——要么全为 0
（无约束盘），要么全为同一个非 0 值。混入任何不同值（含 0 与非 0 混装）均违规。

实现方式：分组隔离（结构性保证）＋ 双层门禁（保险）。
- 分组：``OrderProcessor.group_by_order`` 对非 0 组在销售订单号键上追加内部
  后缀 ``__CASEGROUP__<值>``（复用与组内子聚类 ``__SPLITREST__`` 相同的机制），
  使不同 case_group 永不同组 → 永不同盘；输出前由 workflow 剥离还原。
- 门禁：整盘门禁（constraint_validator）与输出门禁（result_formatter）各做一次
  纯度校验，防未来代码变更破坏隔离。

接口约定（对外）：箱子字典的可选字段 ``case_group``（int/float/str 均可，见
``normalize_case_group`` 的归一化规则）。Excel 本地测试用同名可选列。
"""

from typing import Dict, List, Optional, Tuple, Union

# 内部分组标签：追加在销售订单号之后，输出前剥离。与 workflow._SPLIT_REST_TAG
# 同机制。真实订单号不会包含该串。
CASE_GROUP_ORDER_TAG = '__CASEGROUP__'

CaseGroup = Union[int, str]  # 0 = 无约束；其余为规范化字符串组标识


def normalize_case_group(value) -> CaseGroup:
    """归一化 case_group 取值；0/None/NaN/空串/'0' → 0（无约束）。

    数值型（int/float/数字字符串）统一为整数字符串（1、1.0、'1' → '1'），
    非数字字符串去首尾空白。保证 Excel（浮点列）/JSON（数字或字符串）/未来
    系统接口三种来源的同一组值口径一致。
    """
    if value is None:
        return 0
    if isinstance(value, float) and value != value:  # NaN（pandas 空单元格）
        return 0
    if isinstance(value, (int, float)):
        f = float(value)
        if f == 0:
            return 0
        return str(int(f)) if f.is_integer() else str(f)
    s = str(value).strip()
    if s in ('', '0'):
        return 0
    try:
        f = float(s)
    except ValueError:
        return s
    if f == 0:
        return 0
    return str(int(f)) if f.is_integer() else str(f)


def find_case_group_violation(items: List[Dict]) -> Optional[str]:
    """检查一盘箱子的 case_group 纯度；违规返回描述串，合法返回 None。

    合法 ⇔ 盘内所有箱子归一化 case_group 相同（全 0 或全同一非 0 值）。
    """
    groups = {normalize_case_group(b.get('case_group')) for b in items}
    if len(groups) <= 1:
        return None
    return 'case_group 混装: %s' % sorted(str(g) for g in groups)


def tag_sales_order_no(sales_order_no: str, case_group) -> str:
    """非 0 case_group 时给销售订单号追加内部分组后缀；0 则原样返回。"""
    cg = normalize_case_group(case_group)
    if not cg:
        return sales_order_no
    return f'{sales_order_no}{CASE_GROUP_ORDER_TAG}{cg}'


def split_case_group_tag(sales_order_no: str) -> Tuple[str, CaseGroup]:
    """剥离内部分组后缀，返回 (真实订单号, case_group)；无标签返回 (原串, 0)。"""
    order = sales_order_no or ''
    if CASE_GROUP_ORDER_TAG not in order:
        return order, 0
    head, _, cg = order.partition(CASE_GROUP_ORDER_TAG)
    return head, (cg or 0)
