"""明确分析要求的确定性约束；不从自由文字猜测字段映射或指标口径。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_FILTER_CLAUSE = re.compile(
    r"(?:过滤条件|筛选条件|过滤|筛选|\bfilter\b)\s*(?:为|是|[:：])?\s*([^。；;\n]+)",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"(?:^|并且|而且|且|\bAND\b|&&|[,，、])\s*"
    r"[\"'“`]?([\w\u4e00-\u9fff]+)[\"'”`]?\s*(==|>=|<=|!=|=|>|<)\s*"
    r"(\"[^\"]*\"|'[^']*'|“[^”]*”|[^,，、。；;\s]+?)"
    r"(?=\s*(?:并且|而且|且|\bAND\b|&&|[,，、。；;]|$))",
    re.IGNORECASE,
)
TOTAL_LABEL = re.compile(r"总计|合计|总额|总量|总[\w\u4e00-\u9fff]{1,12}额|\btotal\b", re.I)


@dataclass(frozen=True, slots=True)
class AnalysisRequirements:
    """绑定用户明确的 AND 条件、数据集及总计交付要求。"""

    filters: tuple[dict[str, Any], ...] = ()
    total: bool = False
    total_column: str | None = None
    dataset_ref: str | None = None
    filter_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filters": list(self.filters),
            "total": self.total,
            "total_column": self.total_column,
            "dataset_ref": self.dataset_ref,
            "filter_error": self.filter_error,
        }

    @classmethod
    def from_dict(cls, value: Any) -> AnalysisRequirements:
        """严格恢复已持久化约束，拒绝无效过滤项或非布尔开关。"""
        if not isinstance(value, dict) or not isinstance(value.get("total"), bool):
            raise ValueError("持久化分析约束无效")
        filters = value.get("filters")
        if not isinstance(filters, list) or any(
            not isinstance(item, dict)
            or set(item) != {"column", "op", "value"}
            or not isinstance(item["column"], str)
            or not item["column"]
            or item["op"] not in {"==", "!=", ">", ">=", "<", "<="}
            or not isinstance(item["value"], str | int | float | bool)
            for item in filters
        ):
            raise ValueError("持久化筛选条件无效")
        for key in ("total_column", "dataset_ref", "filter_error"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError("持久化分析作用域无效")
        return cls(
            tuple(filters),
            value["total"],
            value.get("total_column"),
            value.get("dataset_ref"),
            value.get("filter_error"),
        )


def explicit_analysis_requirements(
    text: str, *, columns: tuple[str, ...] = (), dataset_refs: tuple[str, ...] | None = None
) -> AnalysisRequirements:
    """识别明确的比较式 AND 条件与总计请求；不解释 OR 或隐含条件。"""
    filters: list[dict[str, Any]] = []
    filter_error = None
    for clause in _FILTER_CLAUSE.finditer(text):
        expression = clause[1]
        if re.search(r"\bOR\b|或者|或|\|\|", expression, re.I):
            if re.search(r"[=<>!]", expression):
                filter_error = "当前比较式筛选只支持完整的 AND 条件，请澄清 OR 条件。"
            continue
        matches = list(_CONDITION.finditer(expression))
        remainder = expression
        for match in reversed(matches):
            remainder = remainder[: match.start()] + remainder[match.end() :]
        if re.search(r"[=<>!]", remainder):
            filter_error = "部分比较式筛选无法解析，请重述完整条件。"
        for match in matches:
            column, operator, raw = match.groups()
            if raw[0] in "\"'“":
                value: Any = raw[1:-1]
            else:
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
                if value is None or isinstance(value, list | dict):
                    value = raw
            item = {"column": column, "op": "==" if operator == "=" else operator, "value": value}
            if item not in filters:
                filters.append(item)
    total = any(
        not re.search(
            r"(?:不要|无需|不需要|不用|do not|don't)\s*(?:计算|回答|给出|报告)?\s*$",
            text[max(0, match.start() - 16) : match.start()],
            re.I,
        )
        for match in TOTAL_LABEL.finditer(text)
    )
    total_columns = [
        column
        for column in columns
        if re.search(re.escape(column) + r"\s*(?:的\s*)?(?:总计|合计|总额|总量)", text)
        or f"总{column}" in text
    ]
    # Host 已解析的类型化引用优先；绝不把 Artifact/Analysis ID 当作 Dataset ID。
    refs = (
        set(dataset_refs)
        if dataset_refs is not None
        else set(
            re.findall(r"(?:\bdataset_ref\s*[=:：]|数据集\s*)\s*([0-9a-f]{32})(?![0-9a-f])", text)
        )
    )
    if len(refs) > 1 and (total or filters):
        filter_error = "分析目标包含多个数据集，请明确本次指标和筛选使用哪个数据集。"
    if not refs and (total or filters) and re.search(r"[0-9a-f]{32}", text):
        filter_error = "不透明引用缺少已解析的数据集绑定，请先澄清目标。"
    if total and columns and len(total_columns) != 1:
        mentioned = [column for column in columns if column in text]
        if len(mentioned) == 1:
            total_columns = mentioned
        else:
            filter_error = "总计指标无法唯一绑定，请明确指标字段。"
    return AnalysisRequirements(
        tuple(filters),
        bool(total),
        total_columns[0] if len(total_columns) == 1 else None,
        next(iter(refs)) if len(refs) == 1 else None,
        filter_error,
    )


def filter_arguments_match(requirements: AnalysisRequirements, arguments: dict[str, Any]) -> bool:
    """严格对照持久化条件，防止增加、删除或替换过滤条件。"""
    actual = arguments.get("filters", [])
    return (
        isinstance(actual, list)
        and len(actual) == len(requirements.filters)
        and all(item in actual for item in requirements.filters)
        and (
            requirements.dataset_ref is None
            or arguments.get("dataset_ref") == requirements.dataset_ref
        )
    )
