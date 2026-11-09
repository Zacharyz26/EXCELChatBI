"""明确分析要求的确定性约束；不从自由文字猜测字段映射或指标口径。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from packages.common.identifiers import explicit_resource_reference_spans
from packages.common.text_delimiters import delimited_spans, paired_request_examples
from packages.knowledge.formula import normalize_semantic_key

from .context_selection import contextual_field_requested
from .operation_intent import (
    DATA_OPERATION_ACTIONS,
    TOTAL_OPERATION_ACTIONS,
    operation_family,
    operation_mentions,
)

if TYPE_CHECKING:
    from .knowledge_intent import KnowledgeQuery


_COMPARISON_OPERATORS = {
    "大于等于": ">=",
    "小于等于": "<=",
    "不等于": "!=",
    "等于": "==",
    "大于": ">",
    "小于": "<",
    "==": "==",
    ">=": ">=",
    "<=": "<=",
    "!=": "!=",
    "=": "==",
    ">": ">",
    "<": "<",
    "≥": ">=",
    "≤": "<=",
    "≠": "!=",
}
_COMPARISON_OPERATOR_PATTERN = "|".join(
    re.escape(value) for value in sorted(_COMPARISON_OPERATORS, key=len, reverse=True)
)


def comparison_matches(
    text: str,
    *,
    natural_equal: bool = False,
    following_boundary: str = "",
    columns: tuple[str, ...] = (),
) -> list[re.Match[str]]:
    """One finite AND lexer; unquoted fallback cannot consume broken quotes."""
    known = "|".join(re.escape(field) for field in sorted(columns, key=len, reverse=True))
    field = (
        r""""[^"\n]+"|'[^'\n]+'|“[^”\n]+”|`[^`\n]+`|"""
        + (known + "|" if known else "")
        + r"[\w（）()]+?"
    )
    operator = _COMPARISON_OPERATOR_PATTERN + ("|为|是" if natural_equal else "")
    connector = r"并且|而且|且|\bAND\b|&&"
    boundary = r"并且|而且|且|\bAND\b|&&|[,，、。；;]" + (
        "|" + following_boundary if following_boundary else ""
    )
    pattern = (
        rf"(?:^|[,，、]|{connector})\s*(?:(?:{connector})\s*)?"
        + "("
        + field
        + r")\s*("
        + operator
        + r")\s*"
        + r"""("[^"\n]*"|'[^'\n]*'|“[^”\n]*”|[^"'“”` ,，、。；;\s]+?)"""
        + rf"(?=\s*(?:{boundary}|$))"
    )
    return list(re.finditer(pattern, text, re.IGNORECASE))


def comparison_value(match: re.Match[str]) -> dict[str, Any]:
    """同一字段、操作符和字面值契约供任务约束与事实范围核对复用。"""
    column, operator, raw = match.groups()
    if column[0] in "\"'“`":
        column = column[1:-1]
    if raw[0] in "\"'“":
        value: Any = raw[1:-1]
    else:
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        if value is None or isinstance(value, list | dict):
            value = raw
    return {
        "column": column,
        "op": "==" if operator in {"为", "是"} else _COMPARISON_OPERATORS[operator],
        "value": value,
    }


TOTAL_LABEL = re.compile(r"总计|合计|总额|总量|总[\w\u4e00-\u9fff]{1,12}额|\btotal\b", re.I)


OperationKind = Literal["filter", "drop_nulls", "drop_duplicates", "aggregate"]
OperationDisposition = Literal[
    "current", "prohibited", "deferred", "described", "absent", "cancelled"
]
_KINDS = {"filter", "drop_nulls", "drop_duplicates", "aggregate"}
_DISPOSITIONS = {"current", "prohibited", "deferred", "described", "absent", "cancelled"}
_SLOTS = {
    "dataset",
    "filters",
    "operation",
    "columns",
    "group_col",
    "value_col",
    "aggregation",
    "time_grain",
}
_REASONS = {
    "missing_dataset",
    "ambiguous_dataset",
    "invalid_filter",
    "missing_column",
    "ambiguous_operation",
    "operation_columns_required",
    "group_required",
    "metric_required",
    "aggregation_required",
    "role_confirmation_required",
    "unsupported_time_grain",
    "unsupported_row_count",
    "unsupported_aggregation",
}
_AGGREGATIONS = {"sum", "mean", "count"}


@dataclass(frozen=True, slots=True)
class FilterCondition:
    column: str
    op: str
    value: str | int | float | bool


@dataclass(frozen=True, slots=True)
class OperationParameters:
    filters: tuple[FilterCondition, ...] = ()
    columns: tuple[str, ...] = ()
    whole_row: bool = False
    definition_key: str | None = None


@dataclass(frozen=True, slots=True)
class OperationRequirement:
    operation_id: str
    kind: OperationKind
    disposition: OperationDisposition
    parameters: OperationParameters
    span: tuple[int, int]
    context_span: tuple[int, int]
    order: int


@dataclass(frozen=True, slots=True)
class RequirementClarification:
    slot: str
    reason: str
    candidates: tuple[str, ...] = ()
    operation_id: str | None = None
    allowed_roles: tuple[str, ...] = ()
    requires_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class ColumnRoleBinding:
    column: str
    primary_role: str
    candidate_roles: tuple[str, ...] = ()
    requires_confirmation: bool = False


def _stable(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ValueError("分析约束包含非法或非有限数值") from error


def _strings(value: Any, *, empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("分析约束字段列表无效")
    if not empty and not value:
        raise ValueError("分析约束字段列表为空")
    if len(set(value)) != len(value):
        raise ValueError("分析约束字段重复")
    return tuple(value)


def _filter(item: Any) -> FilterCondition:
    if not isinstance(item, dict) or set(item) != {"column", "op", "value"}:
        raise ValueError("持久化筛选条件无效")
    if (
        not isinstance(item["column"], str)
        or not item["column"]
        or not isinstance(item["op"], str)
        or item["op"] not in {"==", "!=", ">", ">=", "<", "<="}
    ):
        raise ValueError("持久化筛选条件无效")
    if not isinstance(item["value"], str | int | float | bool) or (
        isinstance(item["value"], float) and not math.isfinite(item["value"])
    ):
        raise ValueError("持久化筛选字面值无效")
    return FilterCondition(**item)


def _span(value: Any, length: int) -> tuple[int, int]:
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or any(type(x) is not int for x in value)
        or not 0 <= value[0] < value[1] <= length
    ):
        raise ValueError("分析义务原文位置越界")
    return value[0], value[1]


def _filter_projection(operations: tuple[OperationRequirement, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        asdict(item)
        for operation in operations
        if operation.kind == "filter" and operation.disposition == "current"
        for item in operation.parameters.filters
    )


@dataclass(frozen=True, slots=True)
class AnalysisRequirements:
    """One frozen task authority; legacy five-field records stay explicitly v1."""

    filters: tuple[dict[str, Any], ...] = ()
    total: bool = False
    total_column: str | None = None
    dataset_ref: str | None = None
    filter_error: str | None = None
    schema_version: int = 1
    revision: int = 0
    source_hash: str | None = None
    source_length: int | None = None
    operations: tuple[OperationRequirement, ...] = ()
    group_col: str | None = None
    value_col: str | None = None
    aggregation: str | None = None
    time_grain: str | None = None
    clarifications: tuple[RequirementClarification, ...] = ()
    knowledge_queries: tuple[KnowledgeQuery, ...] = ()
    knowledge_prohibitions: tuple[KnowledgeQuery, ...] = ()

    def __post_init__(self) -> None:
        # Compatibility JSON view is copied, never another editable authority.
        object.__setattr__(self, "filters", tuple(dict(item) for item in self.filters))

    @property
    def clarification(self) -> RequirementClarification | None:
        return self.clarifications[0] if self.clarifications else None

    def validate_integrity(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValueError("分析契约版本不受支持")
        if type(self.total) is not bool:
            raise ValueError("总计开关无效")
        for item in self.filters:
            _filter(item)
        if self.schema_version == 1:
            return
        if (
            type(self.revision) is not int
            or self.revision < 0
            or type(self.source_length) is not int
            or self.source_length < 0
        ):
            raise ValueError("分析修订或原文长度无效")
        if (
            not isinstance(self.source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.source_hash) is None
        ):
            raise ValueError("分析原文摘要无效")
        if any(
            not isinstance(getattr(self, key), tuple)
            for key in (
                "operations",
                "clarifications",
                "knowledge_queries",
                "knowledge_prohibitions",
            )
        ):
            raise ValueError("分析权威序列必须冻结")
        for key in (
            "group_col",
            "value_col",
            "total_column",
            "dataset_ref",
            "filter_error",
            "time_grain",
        ):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("分析作用域或字段身份无效")
        if self.aggregation is not None and (
            not isinstance(self.aggregation, str) or self.aggregation not in _AGGREGATIONS
        ):
            raise ValueError("不支持的聚合口径")
        if self.time_grain not in (
            None,
            "month",
            "week",
            "quarter",
            "year",
            "day",
            "column_values",
        ):
            raise ValueError("时间分组口径无效")
        if self.total_column != (self.value_col if self.total else None):
            raise ValueError("总计指标与唯一指标槽不一致")
        ids: set[str] = set()
        for index, operation in enumerate(self.operations):
            if (
                not isinstance(operation, OperationRequirement)
                or not isinstance(operation.kind, str)
                or not isinstance(operation.disposition, str)
                or operation.kind not in _KINDS
                or operation.disposition not in _DISPOSITIONS
            ):
                raise ValueError("分析操作义务无效")
            if type(operation.order) is not int or operation.order != index:
                raise ValueError("分析操作顺序无效")
            if (
                not isinstance(operation.operation_id, str)
                or not re.fullmatch(r"op_[0-9a-f]{32}", operation.operation_id)
                or operation.operation_id in ids
            ):
                raise ValueError("分析操作身份重复或无效")
            ids.add(operation.operation_id)
            span = _span(operation.span, self.source_length)
            context = _span(operation.context_span, self.source_length)
            if not context[0] <= span[0] < span[1] <= context[1]:
                raise ValueError("分析操作不在所属原文范围")
            params = operation.parameters
            if not isinstance(params, OperationParameters) or type(params.whole_row) is not bool:
                raise ValueError("分析操作参数无效")
            if not isinstance(params.filters, tuple) or not isinstance(params.columns, tuple):
                raise ValueError("分析操作参数必须冻结")
            _strings(params.columns)
            for condition in params.filters:
                if not isinstance(condition, FilterCondition):
                    raise ValueError("筛选条件不是冻结值")
                _filter(asdict(condition))
            if operation.kind != "filter" and params.filters:
                raise ValueError("其他操作不能携带筛选条件")
            if operation.kind in {"filter", "aggregate"} and (params.columns or params.whole_row):
                raise ValueError("操作携带不属于它的参数")
            if operation.kind != "drop_duplicates" and params.whole_row:
                raise ValueError("整行口径仅适用于去重")
            if params.whole_row and params.columns:
                raise ValueError("整行和子集去重口径冲突")
            if params.definition_key is not None and (
                operation.kind != "aggregate"
                or not isinstance(params.definition_key, str)
                or normalize_semantic_key(params.definition_key) != params.definition_key
            ):
                raise ValueError("定义依赖必须绑定汇总操作的规范语义键")
        if _stable(self.filters) != _stable(_filter_projection(self.operations)):
            raise ValueError("兼容筛选投影与冻结操作不一致")
        for question in self.clarifications:
            if (
                not isinstance(question, RequirementClarification)
                or not isinstance(question.slot, str)
                or not isinstance(question.reason, str)
                or question.slot not in _SLOTS
                or question.reason not in _REASONS
            ):
                raise ValueError("分析澄清槽无效")
            if not isinstance(question.candidates, tuple) or not isinstance(
                question.allowed_roles, tuple
            ):
                raise ValueError("澄清候选必须冻结")
            _strings(question.candidates)
            _strings(question.allowed_roles)
            if type(question.requires_confirmation) is not bool or (
                question.operation_id is not None and question.operation_id not in ids
            ):
                raise ValueError("分析澄清不属于当前操作")
        from .knowledge_intent import KnowledgeQuery

        for query in (*self.knowledge_queries, *self.knowledge_prohibitions):
            if not isinstance(query, KnowledgeQuery):
                raise ValueError("知识义务必须为冻结对象")
            if not isinstance(query.path, tuple) or type(query.all_sources) is not bool:
                raise ValueError("知识义务对象无效")
            _strings(query.path, empty=False)
            if query.all_sources != (query.path == ("知识库",)):
                raise ValueError("知识义务全库范围与对象不一致")
            if not isinstance(query.source_spans, tuple) or not query.source_spans:
                raise ValueError("知识义务缺少原文来源")
            for span in query.source_spans:
                if not isinstance(span, tuple):
                    raise ValueError("知识义务原文位置必须冻结")
                _span(span, self.source_length)
        if any(
            operation.disposition == "current"
            and operation.parameters.definition_key is not None
            and not any(
                query.path == (operation.parameters.definition_key,) and not query.all_sources
                for query in self.knowledge_queries
            )
            for operation in self.operations
        ):
            raise ValueError("定义驱动操作缺少同对象必需查询")
        if (
            self.total
            and self.value_col is None
            and not any(q.slot == "value_col" for q in self.clarifications)
        ):
            raise ValueError("总计尚无指标且缺少必要澄清")
        if (
            self.aggregation == "count"
            and self.value_col is None
            and not any(
                q.reason == "unsupported_row_count" or q.slot == "value_col"
                for q in self.clarifications
            )
        ):
            raise ValueError("全行计数不能降级为未指定度量的非空计数")

    def validate_source(self, goal: str) -> None:
        self.validate_integrity()
        if self.schema_version == 2 and (
            len(goal) != self.source_length
            or hashlib.sha256(goal.encode("utf-8")).hexdigest() != self.source_hash
        ):
            raise ValueError("分析契约与保存的请求原文不一致")
        if self.schema_version == 2:
            for operation in self.operations:
                key = operation.parameters.definition_key
                if key is not None:
                    operand = _definition_operand(
                        goal, operation.context_span[0], operation.span[0]
                    )
                    if operand is None or operand[0] != key:
                        raise ValueError("定义依赖与原请求操作对象不一致")

    def active_operations(self) -> tuple[OperationRequirement, ...]:
        self.validate_integrity()
        return tuple(item for item in self.operations if item.disposition == "current")

    def requires_operation(self, kind: str) -> bool:
        return any(item.kind == kind for item in self.active_operations())

    def needs_clarification(self) -> bool:
        self.validate_integrity()
        return bool(self.clarifications or self.filter_error)

    def operation_arguments(self, operation_id: str) -> dict[str, Any]:
        self.validate_integrity()
        operation = next(
            (
                item
                for item in self.operations
                if item.operation_id == operation_id and item.disposition == "current"
            ),
            None,
        )
        if operation is None:
            raise ValueError("没有当前获准操作")
        if any(item.operation_id in {None, operation_id} for item in self.clarifications):
            raise ValueError("操作参数仍待澄清")
        if operation.kind == "filter":
            if not operation.parameters.filters:
                raise ValueError("筛选条件尚未绑定")
            return {"filters": [asdict(item) for item in operation.parameters.filters]}
        if operation.kind == "drop_nulls":
            if not operation.parameters.columns:
                raise ValueError("空值处理字段尚未绑定")
            return {"drop_nulls": list(operation.parameters.columns)}
        if operation.kind == "drop_duplicates":
            if not operation.parameters.whole_row and not operation.parameters.columns:
                raise ValueError("去重口径尚未绑定")
            return {"drop_duplicates": list(operation.parameters.columns)}
        if (
            self.group_col is None
            or self.aggregation is None
            or (self.value_col is None and self.aggregation != "count")
            or self.time_grain not in {None, "column_values"}
        ):
            raise ValueError("聚合参数仍待澄清或工具不支持时间分桶")
        return {"group_col": self.group_col, "value_col": self.value_col, "agg": self.aggregation}

    def to_dict(self) -> dict[str, Any]:
        self.validate_integrity()
        result = {
            "filters": [dict(x) for x in self.filters],
            "total": self.total,
            "total_column": self.total_column,
            "dataset_ref": self.dataset_ref,
            "filter_error": self.filter_error,
        }
        if self.schema_version == 1:
            return result
        result.update(
            schema_version=2,
            revision=self.revision,
            source_hash=self.source_hash,
            source_length=self.source_length,
            operations=[asdict(item) for item in self.operations],
            group_col=self.group_col,
            value_col=self.value_col,
            aggregation=self.aggregation,
            time_grain=self.time_grain,
            clarifications=[asdict(item) for item in self.clarifications],
            knowledge_queries=[asdict(item) for item in self.knowledge_queries],
            knowledge_prohibitions=[asdict(item) for item in self.knowledge_prohibitions],
        )
        # JSON container shape is stable for direct callers as well as persistence.
        return cast(dict[str, Any], json.loads(_stable(result)))

    @classmethod
    def from_dict(cls, value: Any) -> AnalysisRequirements:
        if (
            not isinstance(value, dict)
            or type(value.get("total")) is not bool
            or not isinstance(value.get("filters"), list)
        ):
            raise ValueError("持久化分析约束无效")
        version = value.get("schema_version", 1)
        if type(version) is not int or version not in (1, 2):
            raise ValueError("持久化分析版本无效")
        filters = tuple(asdict(_filter(item)) for item in value["filters"])
        for key in ("total_column", "dataset_ref", "filter_error"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError("持久化分析作用域无效")
        if version == 1:
            if set(value) - {
                "filters",
                "total",
                "total_column",
                "dataset_ref",
                "filter_error",
                "schema_version",
            }:
                raise ValueError("v2义务不能降级为旧版合同")
            return cls(
                filters,
                value["total"],
                value.get("total_column"),
                value.get("dataset_ref"),
                value.get("filter_error"),
            )
        allowed = set(cls.__dataclass_fields__)
        if set(value) != allowed:
            raise ValueError("持久化v2分析字段缺失或多余")
        length = value["source_length"]
        if type(length) is not int or length < 0:
            raise ValueError("分析原文长度无效")
        if not isinstance(value["operations"], list) or not isinstance(
            value["clarifications"], list
        ):
            raise ValueError("持久化分析序列无效")
        operations = []
        for raw in value["operations"]:
            if not isinstance(raw, dict) or set(raw) != set(
                OperationRequirement.__dataclass_fields__
            ):
                raise ValueError("持久化操作形状无效")
            params = raw["parameters"]
            if (
                not isinstance(params, dict)
                or set(params) != {"filters", "columns", "whole_row", "definition_key"}
                or not isinstance(params["filters"], list)
            ):
                raise ValueError("持久化操作参数形状无效")
            operations.append(
                OperationRequirement(
                    **{
                        **raw,
                        "span": _span(raw["span"], length),
                        "context_span": _span(raw["context_span"], length),
                        "parameters": OperationParameters(
                            tuple(_filter(item) for item in params["filters"]),
                            _strings(params["columns"]),
                            params["whole_row"],
                            params.get("definition_key"),
                        ),
                    }
                )
            )
        questions = []
        for raw in value["clarifications"]:
            if not isinstance(raw, dict) or set(raw) != set(
                RequirementClarification.__dataclass_fields__
            ):
                raise ValueError("持久化澄清形状无效")
            questions.append(
                RequirementClarification(
                    **{
                        **raw,
                        "candidates": _strings(raw["candidates"]),
                        "allowed_roles": _strings(raw["allowed_roles"]),
                    }
                )
            )
        from .knowledge_intent import KnowledgeQuery

        def queries(raw: Any) -> tuple[KnowledgeQuery, ...]:
            if not isinstance(raw, list):
                raise ValueError("知识义务列表无效")
            parsed = []
            for item in raw:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "all_sources", "source_spans"}
                    or not isinstance(item["source_spans"], list)
                ):
                    raise ValueError("知识义务形状无效")
                parsed.append(
                    KnowledgeQuery(
                        _strings(item["path"]),
                        item["all_sources"],
                        tuple(_span(span, length) for span in item["source_spans"]),
                    )
                )
            return tuple(parsed)

        result = cls(
            **{
                **value,
                "filters": filters,
                "operations": tuple(operations),
                "clarifications": tuple(questions),
                "knowledge_queries": queries(value["knowledge_queries"]),
                "knowledge_prohibitions": queries(value["knowledge_prohibitions"]),
            }
        )
        result.validate_integrity()
        return result


def _parse_filter(
    expression: str, columns: tuple[str, ...], *, natural_equal: bool = False
) -> tuple[tuple[FilterCondition, ...], str | None]:
    connector_view = list(expression)
    for left, right in delimited_spans(expression):
        if expression[left] in "\"'`“‘「『":
            connector_view[left:right] = " " * (right - left)
    if re.search(r"\bOR\b|或者|或|\|\|", "".join(connector_view), re.I):
        return (), "当前筛选仅支持完整AND条件，请澄清OR条件。"
    expression = re.sub(r"^\s*(?:条件)?\s*(?:为|是|[:：])?\s*", "", expression)
    matches = comparison_matches(expression, columns=columns, natural_equal=natural_equal)
    remainder = list(expression)
    values: list[FilterCondition] = []
    for match in matches:
        remainder[match.start() : match.end()] = " " * len(match[0])
        item = _filter(comparison_value(match))
        if all(_stable(asdict(item)) != _stable(asdict(prior)) for prior in values):
            values.append(item)
    tail = "".join(remainder).strip(" ，,、。；;\t")
    # Only explicitly non-condition explanatory tails are outside the filter.
    # Unconsumed conjunctions always block, even when the second operator is absent.
    if tail and not re.fullmatch(
        r"(?:说明|报告|给出)(?:筛选)?(?:前|后|前后)[^,，、;；。\n=<>!]*(?:行数|记录数)", tail
    ):
        return tuple(values), "部分筛选条件无法完整绑定，请重述全部AND条件。"
    if not values:
        return (), "筛选条件尚未明确。"
    if columns and any(item.column not in columns for item in values):
        return tuple(values), "筛选字段不存在，请选择当前数据集的精确字段。"
    if any(
        not match[3].startswith(('"', "'", "“")) and re.search(r"的(?:记录|数据|行)$", match[3])
        for match in matches
    ):
        return tuple(values), "未加引号的值与记录范围描述边界不明确，请明确完整条件。"
    return tuple(values), None


def _parse_retained_filter(
    expression: str, columns: tuple[str, ...]
) -> tuple[tuple[FilterCondition, ...], str | None]:
    """保留谓语的条件从句；记录宾语不是比较字面值，不裁剪引号内值。"""
    view = _boundary_view(expression)
    relative = re.search(r"的[^的，,。；;\n]*(?:记录|行)\s*(?=[，,]|$)", view)
    if relative is not None:
        tail = view[relative.end() :].strip(" ，,")
        # This tail is a result-description clause, not another predicate or
        # permission to omit a partially parsed conjunction.
        if tail and re.fullmatch(r"(?:并)?(?:说明|描述)(?:筛选|过滤)?结果", tail) is None:
            return (), "保留条件后的要求尚未完整归属，请明确完整筛选范围。"
        expression = expression[: relative.start()]
    return _parse_filter(expression, columns, natural_equal=True)


def parse_filter_answer(answer: str, columns: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    """Parse a complete explicit clarification using the same finite lexer."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("请提供完整筛选条件")
    expression = re.sub(
        r"^(?:过滤|筛选)(?:条件)?\s*(?:为|是|[:：])?\s*", "", answer.strip()
    ).rstrip("。；; \n")
    conditions, error = _parse_filter(expression, columns)
    if error:
        raise ValueError(error)
    return tuple(asdict(item) for item in conditions)


def _column_mentions(text: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Longest exact nonoverlapping schema identities, preserving source order."""
    if not columns:
        return ()
    pattern = re.compile(
        "|".join(re.escape(name) for name in sorted(columns, key=len, reverse=True))
    )
    return tuple(dict.fromkeys(match[0] for match in pattern.finditer(text)))


def _intent_view(text: str, columns: tuple[str, ...]) -> str:
    """Schema names are instruction operands, not polarity/action tokens."""
    if not columns:
        return text
    view = list(text)
    pattern = re.compile(
        "|".join(re.escape(name) for name in sorted(columns, key=len, reverse=True))
    )
    for match in pattern.finditer(text):
        view[match.start() : match.end()] = " " * len(match[0])
    return "".join(view)


def _boundary_view(text: str) -> str:
    view = list(text)
    for left, right in delimited_spans(text):
        if text[left] in "\"'`“‘「『":
            view[left:right] = " " * (right - left)
    return "".join(view)


def _unowned_comparisons(
    text: str, columns: tuple[str, ...], operations: list[OperationRequirement]
) -> tuple[tuple[int, int], ...]:
    """Every explicit comparison needs an instruction owner, including tails.

    Statement splitting is not evidence that a later condition was withdrawn.
    Only recognized filter contexts, closed quotations/examples and the
    existing explicit resource-identity syntax explain a comparison outside the
    parser.  An identically named dataset column keeps business-field ownership.
    """
    view = list(_intent_view(paired_request_examples(text), columns))
    closers = dict(zip("\"'`“‘「『", "\"'`”’」』", strict=True))
    for left, right in delimited_spans(text):
        if text[left] in closers and right - left >= 2 and text[right - 1] == closers[text[left]]:
            view[left:right] = " " * (right - left)
    for left, right in explicit_resource_reference_spans(text):
        header = re.split(r"[:=]", text[left:right], maxsplit=1)[0].rstrip()
        operand_end = left + len(header)
        # A full schema identity may include the resource-looking suffix, e.g.
        # a prefixed/dotted field.  A short column such as `id` or `analysis`
        # does not own the complete explicit reference header.
        schema_owned = any(
            operand_end - len(column) <= left and text[:operand_end].endswith(column)
            for column in columns
        )
        if not schema_owned:
            view[left:right] = " " * (right - left)
    owners = [item.context_span for item in operations if item.kind == "filter"]
    return tuple(
        match.span()
        for match in re.finditer(_COMPARISON_OPERATOR_PATTERN, "".join(view))
        if not any(left <= match.start() < match.end() <= right for left, right in owners)
    )


def _has_object(operation: OperationRequirement) -> bool:
    return bool(
        operation.parameters.filters
        or operation.parameters.columns
        or operation.parameters.whole_row
        or operation.parameters.definition_key
    )


def _definition_operand(
    text: str, start: int, action_start: int
) -> tuple[str, tuple[int, int]] | None:
    """Read only an explicit canonical key operand of '按 <key> 汇总'."""
    match = re.search(r"按(?:照)?\s*(\S+)\s*$", text[start:action_start])
    if match is None:
        return None
    raw = match[1]
    left, right = start + match.start(1), start + match.end(1)
    closers = {'"': '"', "'": "'", "`": "`", "“": "”"}
    if raw[0] in closers:
        if len(raw) < 3 or raw[-1] != closers[raw[0]]:
            return None
        raw, left, right = raw[1:-1], left + 1, right - 1
    try:
        return normalize_semantic_key(raw), (left, right)
    except ValueError:
        return None


def _same_object(left: OperationRequirement, right: OperationRequirement) -> bool:
    return left.kind == right.kind and _stable(asdict(left.parameters)) == _stable(
        asdict(right.parameters)
    )


def _fold_operations(
    text: str,
    operations: list[OperationRequirement],
) -> tuple[list[OperationRequirement], bool]:
    """Fold only resolved operation objects; ambiguous revisions stay blocked."""
    folded: list[OperationRequirement] = []
    ambiguous = False
    for operation in operations:
        prefix = text[operation.context_span[0] : operation.span[0]]
        revision = operation.disposition == "current" and bool(
            re.search(r"改为|改口|改成|调整为|现在执行", prefix)
        )
        if operation.disposition == "cancelled" or revision:
            peers = [
                i
                for i, old in enumerate(folded)
                if old.kind == operation.kind
                and old.disposition not in {"cancelled", "described", "absent"}
            ]
            exact = [
                i for i in peers if _has_object(operation) and _same_object(folded[i], operation)
            ]
            wildcard = [i for i in peers if not _has_object(folded[i])]
            chosen = exact or (wildcard if revision else [])
            if not chosen and len(peers) == 1:
                old = folded[peers[0]]
                if not _has_object(operation) or (revision and old.disposition == "current"):
                    chosen = peers
            if not chosen and peers:
                ambiguous = True
            for index in chosen:
                folded[index] = replace(folded[index], disposition="cancelled")
        folded.append(operation)
    return folded, ambiguous


def explicit_analysis_requirements(
    text: str,
    *,
    columns: tuple[str, ...] = (),
    dataset_refs: tuple[str, ...] | None = None,
    column_roles: tuple[ColumnRoleBinding, ...] = (),
) -> AnalysisRequirements:
    """Compile a bounded request once; unknown slots remain obligations, not guesses."""
    from .knowledge_intent import (
        KnowledgeQuery,
        knowledge_query_requirements,
        knowledge_scope_covers,
    )

    text = text.strip()
    _strings(columns)
    role_map = {item.column: item for item in column_roles}
    if len(role_map) != len(column_roles) or any(name not in columns for name in role_map):
        raise ValueError("角色候选不属于当前数据集字段")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    boundary_view = _boundary_view(text)
    instruction_view = _intent_view(paired_request_examples(text), columns)
    mentions = operation_mentions(instruction_view, actions=DATA_OPERATION_ACTIONS)
    operations: list[OperationRequirement] = []
    questions: list[RequirementClarification] = []
    filter_errors: dict[str, str] = {}
    definition_spans: dict[str, tuple[int, int]] = {}

    def question(
        slot: str,
        reason: str,
        candidates: tuple[str, ...] = (),
        operation_id: str | None = None,
        allowed_roles: tuple[str, ...] = (),
    ) -> None:
        item = RequirementClarification(slot, reason, candidates, operation_id, allowed_roles)
        if item not in questions:
            questions.append(item)

    for index, mention in enumerate(mentions):
        kind = cast(OperationKind, operation_family(mention.action))
        start = max(
            (m.end() for m in re.finditer(r"[。；;，,\n]", boundary_view[: mention.span[0]])),
            default=0,
        )
        if operations and operations[-1].context_span[1] <= mention.span[0]:
            start = max(start, operations[-1].context_span[1])
        end_match = re.search(r"[。；;\n]", boundary_view[mention.span[1] :])
        end = mention.span[1] + end_match.start() if end_match else len(text)
        # A later explicit action starts a distinct operation; never combine them.
        next_mention = next(
            (
                m
                for m in mentions[index + 1 :]
                if m.span[0] < end
                and m.disposition in {"current", "cancelled", "prohibited", "deferred"}
            ),
            None,
        )
        if next_mention is not None:
            separators = list(
                re.finditer(
                    r"[,，]|(?:然后|再|并且|并)\s*$",
                    boundary_view[mention.span[1] : next_mention.span[0]],
                )
            )
            if separators:
                end = mention.span[1] + separators[-1].start()
        local = text[start:end]
        identity = (
            "op_"
            + hashlib.sha256(
                f"{digest}:{kind}:{mention.span}:{len(operations)}".encode()
            ).hexdigest()[:32]
        )
        params = OperationParameters()
        if mention.disposition in {"current", "cancelled", "prohibited", "deferred"}:
            if kind == "filter":
                expr = text[mention.span[1] : end]
                filters, error = (
                    _parse_retained_filter(expr, columns)
                    if mention.action == "保留"
                    else _parse_filter(expr, columns)
                )
                params = OperationParameters(filters=filters)
                if error and mention.disposition == "current":
                    filter_errors[identity] = error
                    question("filters", "invalid_filter", columns, identity)
            elif kind == "drop_nulls":
                null_action = re.search(r"空值|为空|缺失|null", local, re.I)
                if not null_action and mention.disposition == "current":
                    question(
                        "operation",
                        "ambiguous_operation",
                        ("drop_nulls", "drop_duplicates"),
                        identity,
                    )
                targets = _column_mentions(local, columns)
                params = OperationParameters(columns=targets)
                if not targets and mention.disposition == "current":
                    question("columns", "operation_columns_required", columns, identity)
            elif kind == "drop_duplicates":
                whole = bool(re.search(r"整行|全行|所有列|全部列", local))
                targets = () if whole else _column_mentions(local, columns)
                params = OperationParameters(columns=targets, whole_row=whole)
                if not whole and not targets and mention.disposition == "current":
                    question("columns", "operation_columns_required", columns, identity)
            elif kind == "aggregate":
                operand = _definition_operand(text, start, mention.span[0])
                if operand is not None and (
                    operand[0] not in columns and text[operand[1][0] : operand[1][1]] not in columns
                ):
                    params = OperationParameters(definition_key=operand[0])
                    definition_spans[identity] = operand[1]
        operations.append(
            OperationRequirement(
                identity,
                kind,
                mention.disposition,
                params,
                mention.span,
                (start, max(end, mention.span[1])),
                len(operations),
            )
        )
        if mention.action == "清洗" and mention.disposition != "current":
            # The existing tool's cleanup family has exactly these two actions.
            # A generic non-execution request covers both without making a
            # dedup-specific request a wildcard ban on unrelated transforms.
            sibling_id = (
                "op_"
                + hashlib.sha256(
                    f"{digest}:drop_duplicates:{mention.span}:{len(operations)}".encode()
                ).hexdigest()[:32]
            )
            operations.append(
                replace(
                    operations[-1],
                    operation_id=sibling_id,
                    kind="drop_duplicates",
                    order=len(operations),
                )
            )
    operations, ambiguous_revision = _fold_operations(instruction_view, operations)
    if ambiguous_revision:
        question("operation", "ambiguous_operation")
    # Adjacent grouping words may describe one operation; separated requests
    # are never collapsed into a single unrepresentable method/field slot.
    aggregate_seen = False
    compact: list[OperationRequirement] = []
    for operation in operations:
        if operation.kind == "aggregate" and operation.disposition == "current":
            if aggregate_seen:
                previous = next(
                    item
                    for item in reversed(compact)
                    if item.kind == "aggregate" and item.disposition == "current"
                )
                if not text[previous.span[1] : operation.span[0]].strip():
                    continue
                question("operation", "ambiguous_operation")
            aggregate_seen = True
        compact.append(replace(operation, order=len(compact)))
    operations = compact
    if _unowned_comparisons(text, columns, operations):
        current_filters = [
            item for item in operations if item.kind == "filter" and item.disposition == "current"
        ]
        if len(current_filters) == 1:
            owner = current_filters[0].operation_id
            filter_errors[owner] = "原文含尚未归属的显式条件，请确认完整筛选范围。"
            question("filters", "invalid_filter", columns, owner)
        else:
            question("operation", "ambiguous_operation")
    total_mentions = operation_mentions(instruction_view, actions=TOTAL_OPERATION_ACTIONS)
    total = False
    for mention in total_mentions:
        if mention.disposition == "cancelled":
            total = False
        elif mention.disposition == "current":
            total = True
    if not total_mentions:
        total = any(
            not re.search(
                r"(?:不要|无需|不需要|不用|不必|do not|don't)\s*(?:计算|回答|给出|报告)?\s*$",
                text[max(0, m.start() - 16) : m.start()],
                re.I,
            )
            for m in TOTAL_LABEL.finditer(text)
        )
    active = [item for item in operations if item.disposition == "current"]
    active_ids = {item.operation_id for item in active}
    questions = [q for q in questions if q.operation_id is None or q.operation_id in active_ids]
    filter_errors = {key: value for key, value in filter_errors.items() if key in active_ids}
    for operation_kind in _KINDS:
        if any(item.kind == operation_kind for item in active) and any(
            item.kind == operation_kind and item.disposition in {"prohibited", "deferred"}
            for item in operations
        ):
            question("operation", "ambiguous_operation")
    analysis = total or any(item.kind == "aggregate" for item in active)
    group = value = aggregation = grain = None
    metric_roles = tuple(
        name
        for name, role in role_map.items()
        if role.primary_role == "metric" and not role.requires_confirmation
    )
    group_roles = tuple(
        name
        for name, role in role_map.items()
        if role.primary_role in {"dimension", "time"} and not role.requires_confirmation
    )
    if analysis:
        aggregate_fragments = [
            text[item.context_span[0] : item.context_span[1]]
            for item in active
            if item.kind == "aggregate"
        ]
        fragments = list(aggregate_fragments)
        for mention in total_mentions:
            if mention.disposition != "current":
                continue
            left = max(
                (m.end() for m in re.finditer(r"[。；;，,\n]", boundary_view[: mention.span[0]])),
                default=0,
            )
            right_match = re.search(r"[。；;，,\n]", boundary_view[mention.span[1] :])
            right = mention.span[1] + right_match.start() if right_match else len(text)
            fragments.append(text[left:right])
        analysis_text = "；".join(fragments) if fragments else text
        # A total is a sum obligation, not an instruction to derive its method
        # from adjacent delivery requests such as a post-cleanup row count.
        # Method operands belong to the current aggregation operation itself.
        method_view = _intent_view(
            "；".join(aggregate_fragments) if aggregate_fragments else analysis_text, columns
        )
        non_null_count = bool(re.search(r"非空(?:值|记录)?(?:数量|计数|数|个数)", method_view))
        row_count_requested = (
            bool(aggregate_fragments)
            and not non_null_count
            and bool(re.search(r"记录(?:数量|条数|数)|行数|行数量", method_view))
        )
        unsupported_method = bool(
            re.search(
                r"最大(?:值)?|最小(?:值)?|中位数|分位数|百分位|标准差|方差|\b(?:max|min|median|quantile|std|variance)\b",
                method_view,
                re.I,
            )
        )
        mentioned_columns = _column_mentions(analysis_text, columns)
        groups = [
            name
            for name in mentioned_columns
            if re.search(r"(?:按|以)\s*[`\"“]?" + re.escape(name) + r"[`\"”]?", analysis_text)
        ]
        group = groups[0] if len(groups) == 1 else None
        grain_match = re.search(r"按(月|周|季度|年|日)(?![份度])", analysis_text)
        if grain_match:
            grain = {"月": "month", "周": "week", "季度": "quarter", "年": "year", "日": "day"}[
                grain_match[1]
            ]
            question("time_grain", "unsupported_time_grain", ("column_values",))
        if aggregate_seen and group is None:
            question(
                "group_col", "group_required", group_roles, allowed_roles=("dimension", "time")
            )
        elif group is not None and role_map and (group not in group_roles):
            question(
                "group_col",
                "role_confirmation_required",
                group_roles,
                allowed_roles=("dimension", "time"),
            )
            group = None
        named = [name for name in mentioned_columns if name != group]
        # Scope/filter columns do not become metric candidates by mere mention.
        explicitly_total = [
            name
            for name in mentioned_columns
            if re.search(re.escape(name) + r"\s*(?:的\s*)?(?:总计|合计|总额|总量)", analysis_text)
            or f"总{name}" in analysis_text
        ]
        aggregate_named: list[str] = []
        for operation in active:
            if operation.kind == "aggregate":
                fragment = text[operation.span[1] : operation.context_span[1]]
                aggregate_named.extend(
                    name for name in _column_mentions(fragment, columns) if name != group
                )
        # A dimension mentioned in a grouped total describes the grain; it
        # cannot replace an explicit aggregate operand. If the request names
        # only that dimension as the measured operand, preserve clarification.
        if aggregate_named and group in explicitly_total:
            explicitly_total = [name for name in explicitly_total if name != group]
        named = list(
            dict.fromkeys(
                explicitly_total
                or aggregate_named
                or [
                    name
                    for name in named
                    if name
                    not in {f.column for operation in active for f in operation.parameters.filters}
                ]
            )
        )
        if non_null_count:
            owners = [
                name
                for name in mentioned_columns
                if re.search(
                    re.escape(name) + r"\s*(?:的)?\s*非空(?:值|记录)?(?:数量|计数|数|个数)",
                    analysis_text,
                )
            ]
            value = owners[0] if len(owners) == 1 else None
            if value is None:
                question("value_col", "metric_required", columns)
        elif row_count_requested:
            value = None
            question("aggregation", "unsupported_row_count")
            if total:
                question("value_col", "metric_required", metric_roles, allowed_roles=("metric",))
        elif len(named) == 1 and (not role_map or named[0] in metric_roles):
            value = named[0]
        elif (
            not named
            and len(metric_roles) == 1
            and not contextual_field_requested(analysis_text, columns)
        ):
            value = metric_roles[0]
        else:
            question("value_col", "metric_required", metric_roles, allowed_roles=("metric",))
        aggregation = (
            "count"
            if row_count_requested or non_null_count
            else "mean"
            if re.search(r"平均|均值", method_view)
            else "count"
            if aggregate_fragments and re.search(r"(?:计数|统计条数|记录数量)", method_view)
            else "sum"
        )
        if total and aggregation != "sum":
            question("aggregation", "aggregation_required", ("sum",))
        if unsupported_method:
            aggregation = None
            question("aggregation", "unsupported_aggregation", ("sum", "mean", "count"))
    refs = (
        set(dataset_refs)
        if dataset_refs is not None
        else set(
            re.findall(r"(?:\bdataset_ref\s*[=:：]|数据集\s*)\s*([0-9a-f]{32})(?![0-9a-f])", text)
        )
    )
    if len(refs) > 1 and (active or total):
        question("dataset", "ambiguous_dataset", tuple(sorted(refs)))
    elif not refs and (active or total) and re.search(r"[0-9a-f]{32}", text):
        question("dataset", "missing_dataset")
    intent = knowledge_query_requirements(
        text, data_operation_spans=tuple(item.context_span for item in operations)
    )
    queries = list(intent.active)
    for operation in active:
        key = operation.parameters.definition_key
        if key is None:
            continue
        dependency = KnowledgeQuery((key,), False, (definition_spans[operation.operation_id],))
        if any(knowledge_scope_covers(ban, dependency) for ban in intent.prohibited):
            question("operation", "ambiguous_operation", operation_id=operation.operation_id)
        prior = next((q for q in queries if q.path == dependency.path and not q.all_sources), None)
        if prior is None:
            queries.append(dependency)
        else:
            queries[queries.index(prior)] = replace(
                prior,
                source_spans=tuple(sorted(set((*prior.source_spans, *dependency.source_spans)))),
            )
    result = AnalysisRequirements(
        filters=_filter_projection(tuple(operations)),
        total=total,
        total_column=value if total else None,
        dataset_ref=next(iter(refs)) if len(refs) == 1 else None,
        filter_error=" ".join(filter_errors.values()) or None,
        schema_version=2,
        source_hash=digest,
        source_length=len(text),
        operations=tuple(operations),
        group_col=group,
        value_col=value,
        aggregation=aggregation,
        time_grain=grain,
        clarifications=tuple(questions),
        knowledge_queries=tuple(sorted(queries, key=lambda query: min(query.source_spans))),
        knowledge_prohibitions=intent.prohibited,
    )
    result.validate_source(text)
    return result


def resolve_analysis_slot(
    requirements: AnalysisRequirements,
    *,
    slot: str,
    answer: Any,
    operation_id: str | None = None,
    expected_revision: int | None = None,
    columns: tuple[str, ...] = (),
    column_roles: tuple[ColumnRoleBinding, ...] = (),
) -> AnalysisRequirements:
    """Resolve one already frozen question; never compile a one-word answer as a task."""
    requirements.validate_integrity()
    if requirements.schema_version != 2 or (
        expected_revision is not None and expected_revision != requirements.revision
    ):
        raise ValueError("分析澄清版本不匹配")
    matching = [
        q
        for q in requirements.clarifications
        if q.slot == slot and (operation_id is None or q.operation_id == operation_id)
    ]
    if len(matching) != 1:
        raise ValueError("澄清不属于唯一当前未决槽")
    query = matching[0]
    if query.reason == "unsupported_row_count":
        raise ValueError(
            "当前聚合值为指定列非空计数，不能代替全行记录数；请另发明确的指标非空计数任务。"
        )
    if isinstance(answer, str):
        answer = answer.strip()
    if query.candidates and slot not in {"filters", "columns"} and answer not in query.candidates:
        raise ValueError("答案不属于冻结候选")
    changes: dict[str, Any] = {
        "revision": requirements.revision + 1,
        "clarifications": tuple(q for q in requirements.clarifications if q is not query),
    }
    operations = list(requirements.operations)
    if slot in {"group_col", "value_col"}:
        if not isinstance(answer, str) or not answer or (columns and answer not in columns):
            raise ValueError("澄清字段不存在")
        if not query.candidates:
            # Without frozen role permission the resolver cannot invent it.
            allowed = [
                r.column
                for r in column_roles
                if r.primary_role in query.allowed_roles and not r.requires_confirmation
            ]
            if answer not in allowed:
                raise ValueError("尚无同版本字段角色许可")
        changes[slot] = answer
        if slot == "value_col":
            changes["total_column"] = answer if requirements.total else None
    elif slot == "dataset":
        if not isinstance(answer, str) or not answer or answer not in query.candidates:
            raise ValueError("数据集答案未授权")
        changes["dataset_ref"] = answer
    elif slot == "aggregation":
        if answer not in _AGGREGATIONS or (requirements.total and answer != "sum"):
            raise ValueError("聚合口径不能满足总计")
        changes["aggregation"] = answer
    elif slot == "time_grain":
        if answer != "column_values":
            raise ValueError("当前工具不支持该时间分桶")
        changes["time_grain"] = "column_values"
    elif slot == "operation":
        # A frozen local ambiguity can be resolved by its finite operation
        # candidates. Global multi-operation requests are not replaced by one
        # arbitrary action or by recompiling an unrelated answer.
        found = next(
            (i for i, op in enumerate(operations) if op.operation_id == query.operation_id), None
        )
        if (
            found is None
            or answer not in {"drop_nulls", "drop_duplicates"}
            or answer not in query.candidates
        ):
            raise ValueError("此请求不能用一个操作槽表达；请取消后分别提交明确任务，原要求未删除")
        operation = operations[found]
        if operation.disposition != "current" or operation.kind not in {
            "drop_nulls",
            "drop_duplicates",
        }:
            raise ValueError("澄清不能覆盖禁止、延期或其他已冻结操作")
        operations[found] = replace(operation, kind=answer)
        changes["operations"] = tuple(operations)
        changes["filters"] = _filter_projection(tuple(operations))
    elif slot in {"filters", "columns"}:
        found = next(
            (i for i, x in enumerate(operations) if x.operation_id == query.operation_id), None
        )
        if found is None:
            raise ValueError("澄清操作不存在")
        operation = operations[found]
        if slot == "filters":
            if answer in ("取消筛选", "取消过滤"):
                operations[found] = replace(operation, disposition="cancelled")
                changes["operations"] = tuple(operations)
                changes["filters"] = _filter_projection(tuple(operations))
                changes["filter_error"] = (
                    requirements.filter_error
                    if any(q.slot == "filters" for q in changes["clarifications"])
                    else None
                )
                result = replace(requirements, **changes)
                result.validate_integrity()
                return result
            if isinstance(answer, str):
                answer = list(parse_filter_answer(answer, columns))
            if not isinstance(answer, list) or not answer:
                raise ValueError("筛选澄清必须提供完整结构化AND条件")
            conditions = tuple(_filter(x) for x in answer)
            if columns and any(x.column not in columns for x in conditions):
                raise ValueError("筛选字段不存在")
            params = OperationParameters(filters=conditions)
            changes["filter_error"] = None
        else:
            targets = _strings([answer] if isinstance(answer, str) else answer, empty=False)
            if any(x not in query.candidates or (columns and x not in columns) for x in targets):
                raise ValueError("处理字段不在冻结候选")
            params = replace(operation.parameters, columns=targets)
        operations[found] = replace(operation, parameters=params)
        changes["operations"] = tuple(operations)
        changes["filters"] = _filter_projection(tuple(operations))
    else:
        raise ValueError("操作意图仍不明确，须明确新请求，不能猜测执行")
    result = replace(requirements, **changes)
    result.validate_integrity()
    return result


def filter_arguments_match(requirements: AnalysisRequirements, arguments: dict[str, Any]) -> bool:
    """Strict frozen literal comparison, including scalar type identity."""
    try:
        requirements.validate_integrity()
        actual = arguments.get("filters", [])
        return (
            isinstance(actual, list)
            and sorted(_stable(x) for x in actual)
            == sorted(_stable(x) for x in requirements.filters)
            and (
                requirements.dataset_ref is None
                or arguments.get("dataset_ref") == requirements.dataset_ref
            )
        )
    except (ValueError, TypeError):
        return False
