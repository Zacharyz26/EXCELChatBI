"""保留原文位置的计数声明前端；语法解析不代表有工具事实支持。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packages.orchestration.analysis_requirements import comparison_matches, comparison_value

from apps.orchestrator.control.statistic_statements import (
    COUNT_PREDICATE,
    count_object_at,
    row_quantity_object,
)

CHINESE_COUNT = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+")
_DIGITS = "零一二三四五六七八九"
_OPS = {
    "筛选": "filters",
    "过滤": "filters",
    "清洗空值": "drop_nulls",
    "删除空值": "drop_nulls",
    "去空": "drop_nulls",
    "清洗": "transform",
    "去重": "drop_duplicates",
    "排序": "sort",
    "排除行": "exclude_row_indices",
    "变换": "transform",
    "处理": "transform",
    "操作": "transform",
}
_WRAPPERS = r"[\s*`\"'“”‘’「」『』()（）]*"
_ROW_TRANSITION = re.compile(
    r"\s*(?:数据(?:行数|量)?|行数|记录数)?\s*从\s*"
    r"(?P<before>\d+)\s*(?:行|条记录)\s*(?:变为|变成|降为|增加到|减少到)\s*"
    r"(?P<after>\d+)\s*(?:行|条记录)\s*"
)


def _chinese(value: int, *, initial: bool = True) -> str:
    if value < 10:
        return _DIGITS[value]
    for scale, unit in ((100000000, "亿"), (10000, "万"), (1000, "千"), (100, "百"), (10, "十")):
        if value >= scale:
            high, low = divmod(value, scale)
            prefix = ("" if initial and high == 1 and scale == 10 else _chinese(high)) + unit
            return prefix + (
                ("零" if low < scale // 10 else "") + _chinese(low, initial=False) if low else ""
            )
    raise AssertionError("unreachable")


def parse_chinese_count(token: str) -> int | None:
    """只接受规范非负整数计数；比例、分数、近似和歧义保持未知。"""
    text = token.replace("〇", "零").replace("两", "二")
    if not CHINESE_COUNT.fullmatch(text):
        return None
    total = section = digit = 0
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS.index(char)
        else:
            unit = units[char]
            if unit < 10000:
                section += (digit or 1) * unit
            else:
                total += (section + digit) * unit
                section = 0
            digit = 0
    value = total + section + digit
    if value > 999999999999 or _chinese(value) != text:
        return None
    return value


def numeric_clause(text: str, left: int, right: int) -> str:
    """逗号划分并列事实，冒号保留赋值所有者；格式标记只投影为空格。"""
    boundaries = list(re.finditer(r"[，,、；;。!?！？]", text))
    start = max((m.end() for m in boundaries if m.end() <= left), default=0)
    end = min((m.start() for m in boundaries if m.start() >= right), default=len(text))
    return re.sub(r"[*`\"'“”‘’「」『』]", " ", text[start:end])


def chart_delivery_role(text: str, left: int, right: int) -> str | None:
    """识别肯定交付的集合口径，不能借给数据集维度列数量。"""
    prefix = re.split(r"[，,、；;。!?！？]", text[:left])[-1]
    suffix = text[right:]
    member = re.match(r"\s*(?:(?P<dimension>个\s*维度(?:的)?图表)|张\s*图表)", suffix)
    if member is None:
        return None
    delivered = r"(?:已生成|已完成|已绘制)(?:了)?"
    tail = re.split(r"[，,、；;。!?！？]", suffix[member.end() :])[0]
    # Own the complete local assertion. Searching for 已生成 inside 并非已生成
    # would invert polarity; unknown/modal/negative assertions stay unknown.
    prefix = re.sub(r"^\s*#{1,6}\s+", "", prefix)
    affirmative = (
        re.fullmatch(r"\s*(?:本轮|目前)?\s*" + delivered + r"\s*", prefix) and not tail.strip()
    ) or (not prefix.strip() and re.fullmatch(r"\s*(?:均|都)?" + delivered + r"\s*", tail))
    if not affirmative:
        return "unresolved_delivery"
    return "chart_dimensions" if member["dimension"] else "chart_artifacts"


@dataclass(frozen=True)
class OperationQuantity:
    """请求中的操作、时点与计数口径，不包含来自模型的可信数值。"""

    role: str
    operation: str | None
    duplicate: bool = False
    null_rows: bool = False


def operation_quantity(text: str, left: int, right: int) -> OperationQuantity | None:
    """从数值所在局部赋值确定角色，绝不利用数值反推 before/after。"""
    local = numeric_clause(text, left, right)
    starts = [m.end() for m in re.finditer(r"[，,、；;。!?！？]", text) if m.end() <= left]
    local_start = max(starts, default=0)
    prefix = local[: left - local_start]
    suffix = local[right - local_start :]
    ops = [(m.start(), m.end(), op) for word, op in _OPS.items() for m in re.finditer(word, local)]
    ops = [
        m
        for m in ops
        if not any(a <= m[0] and b >= m[1] and b - a > m[1] - m[0] for a, b, _ in ops)
    ]
    operations = {op for _, _, op in ops}
    operation = (
        next(iter(operations)) if len(operations) == 1 else "ambiguous" if operations else None
    )
    transition = _ROW_TRANSITION.fullmatch(local)
    if transition is not None:
        for phase_name in ("before", "after"):
            if transition.span(phase_name) == (left - local_start, right - local_start):
                return OperationQuantity(phase_name, "transform")
    role = None
    before = re.search(
        r"(?:输入|变换前|处理前|操作前|(?:筛选|过滤|清洗空值|删除空值|清洗|去空|去重|排序|排除行)(?:之)?前)",
        prefix,
    )
    if before:
        role = "before"
    after = re.search(
        r"(?:输出|变换后|处理后|操作后|(?:筛选|过滤|清洗空值|删除空值|清洗|去空|去重|排序|排除行)(?:之)?后)",
        prefix,
    )
    if after:
        role = "ambiguous" if role else "after"
    phase = after or before
    if phase:
        # A temporal row assignment owns its operation. A surrounding label
        # (e.g. a result heading) is not another executed operation. Discover
        # role-qualified operators before values, retaining conflicting phases.
        temporal = {
            op
            for word, op in _OPS.items()
            if re.search(re.escape(word) + r"(?:之)?(?:前|后)", prefix)
        }
        temporal.discard("transform") if len(temporal) > 1 else None
        operation = (
            next(iter(temporal)) if len(temporal) == 1 else ("ambiguous" if temporal else None)
        )
    if phase and not re.fullmatch(
        r"\s*(?:的)?\s*(?:(?:数据)?(?:行数|记录数|数据量|记录数量|行|记录))?\s*"
        + COUNT_PREDICATE
        + r"?\s*(?:为|是|等于|[:：=])?\s*",
        prefix[phase.end() :],
    ):
        return None  # A phase qualifier does not turn a max/amount into rows.
    # A removal quantity is distinct from remaining duplicates or participants.
    if role is None and re.search(
        r"(?:删除|移除|剔除|去掉|去除)(?:了)?\s*(?:的)?(?:重复)?(?:记录|行|数量|数)?\s*(?:为|是|[:：])?"
        + _WRAPPERS
        + r"[\d零〇一二两三四五六七八九十百千万亿]",
        local,
    ):
        role = "removed"
    if role is None and re.search(
        r"(?:剩余|保留)" + _WRAPPERS + r"(?:为|了|是|[:：])?" + _WRAPPERS + r"\d", local
    ):
        role = "after"
    if role is None:
        return None
    obj = row_quantity_object(suffix)
    if obj is None:
        return OperationQuantity("unsupported", operation)
    return OperationQuantity(
        role,
        operation,
        obj.basis == "duplicate_excess" or "重复" in local,
        obj.basis == "null_rows",
    )


def count_noun(text: str, left: int, right: int) -> str | None:
    """返回显式数量宾语；未知宾语仍形成义务，不借别的计数证明。"""
    found = count_object_at(text, left, right)
    return found[0] if found else None


def column_quantity_matches(text: str, left: int, right: int, columns: list[str]) -> bool:
    """列数由实际schema投影证明；完整列名列表不能错借另一列集合。"""
    prefix, suffix = text[:left], text[right:]
    if re.match(r"\s*列(?:[，,。；;!?！？]|$)", suffix) is None:
        return False
    plain = re.split(r"[，,。；;!?！？]", prefix)[-1]
    if re.fullmatch(r"\s*(?:(?:仍|共|共有)|(?:列数|字段数)(?:为|是|[:：=])?)\s*", plain):
        return True
    listing = re.search(r"(?:列结构|字段列表)(?:仍)?为[“\"`](?P<fields>[^”\"`]+)[”\"`]\s*$", prefix)
    return bool(listing and re.split(r"[、，,]", listing["fields"]) == columns)


def operation_scope_matches(text: str, operations: dict[str, Any], columns: list[str]) -> bool:
    """核对声明的操作列和比较式；同值不能替代筛选条件或去重键。"""
    for operation, action in (
        ("drop_duplicates", r"去重"),
        ("drop_nulls", r"清洗空值|删除空值|去空|清洗"),
    ):
        if operation not in operations:
            continue
        selected = operations[operation]
        if (
            re.search(r"(?:整行|全行)\s*(?:" + action + ")", text)
            and selected
            and set(selected) != set(columns)
        ):
            return False
        for match in re.finditer(
            r"(?:按|依据|基于)\s*([^：:，,；;。!?！？\n]+?)\s*(?:进行)?(?:" + action + ")", text
        ):
            named = [s.strip(" \t\"'`“”") for s in re.split(r"[、,，]|以及|和", match[1])]
            if named in (["整行"], ["全行"]):
                if selected and set(selected) != set(columns):
                    return False
            elif set(named) != set(selected or columns):
                return False
    # Operation syntax owns these assignments before the shared filter parser.
    # The original quantities remain audited independently; only this scope
    # view removes a fully recognized row transition or exact null-operation.
    scope_view = list(text)
    for clause in re.finditer(r"[^，,、；;。!?！？]+", text):
        if _ROW_TRANSITION.fullmatch(clause[0]):
            scope_view[clause.start() : clause.end()] = " " * len(clause[0])
        for field in operations.get("drop_nulls", []):
            null_action = re.fullmatch(
                r"\s*(?:删除|移除|剔除)(?:了)?\s*[“\"`]?"
                + re.escape(field)
                + r"[”\"`]?\s*为空(?:值)?的(?:数据)?记录(?:后)?\s*",
                clause[0],
            )
            if null_action:
                scope_view[clause.start() : clause.end()] = " " * len(clause[0])
    comparison_text = "".join(scope_view)
    filters = operations.get("filters", [])
    fields = set(columns) | {item["column"] for item in filters}
    # Discover symbolic comparisons before consulting known fields. An unknown
    # field is an unsupported obligation, never an absent condition. Symbolic
    # and natural equality use the same field validation.
    matches = comparison_matches(
        comparison_text,
        natural_equal=True,
        following_boundary="(?:" + "|".join(_OPS) + ")",
        columns=tuple(fields),
    )
    remainder = comparison_text
    for match in reversed(matches):
        remainder = remainder[: match.start()] + remainder[match.end() :]
    if re.search(r"[=<>]|!=", remainder):
        return False
    for match in matches:
        condition = comparison_value(match)
        field = condition["column"]
        if field in {"dataset_ref", "数据集", "data_version_hash", "数据版本"}:
            continue  # identity_matches checks these independently.
        quantity = operation_quantity(text, *match.span(3))
        if quantity is not None and quantity.role in {"before", "after", "removed"}:
            continue  # A row assignment is not an additional filter.
        if field not in fields or condition not in filters:
            return False
    return True
