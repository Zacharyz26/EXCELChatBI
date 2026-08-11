"""Deterministic data-role inference and read-only quality advice.

The advisor consumes only the governed ``DataProfile`` metadata.  It never reads
raw rows, mutates a dataset, or asks a model to guess a role.  The returned
evidence codes are stable enough to persist in Agent Evidence/Artifact payloads.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from typing import Any, Literal, cast

from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile

DataRole = Literal["time", "metric", "dimension", "identifier", "unknown"]
Severity = Literal["low", "medium", "high"]

ROLE_SCHEMA = "chatbi-data-roles-v1"
QUALITY_SCHEMA = "chatbi-data-quality-v1"

_NUMERIC_DTYPES = {"int", "float"}
_TIME_HINTS = (
    "日期",
    "时间",
    "年份",
    "年度",
    "月份",
    "季度",
    "星期",
    "date",
    "time",
    "timestamp",
    "year",
    "month",
    "quarter",
    "week",
    "period",
)
_IDENTIFIER_HINTS = (
    "编号",
    "编码",
    "代码",
    "序号",
    "单号",
    "uuid",
    "guid",
    "identifier",
    "code",
    "_id",
    "id_",
)
_METRIC_HINTS = (
    "销售",
    "收入",
    "利润",
    "成本",
    "金额",
    "价格",
    "数量",
    "销量",
    "订单数",
    "用户数",
    "率",
    "比例",
    "分数",
    "时长",
    "amount",
    "sales",
    "revenue",
    "profit",
    "cost",
    "price",
    "quantity",
    "count",
    "rate",
    "ratio",
    "score",
    "value",
    "duration",
)
_DIMENSION_HINTS = (
    "地区",
    "区域",
    "城市",
    "国家",
    "类别",
    "类型",
    "状态",
    "渠道",
    "产品",
    "部门",
    "等级",
    "级别",
    "region",
    "city",
    "country",
    "category",
    "type",
    "status",
    "channel",
    "product",
    "department",
)
_FREE_TEXT_HINTS = (
    "备注",
    "说明",
    "描述",
    "评论",
    "内容",
    "comment",
    "description",
    "remark",
    "note",
    "content",
)


def infer_data_roles(profile: DataProfile) -> dict[str, Any]:
    """Infer one primary role per column with candidates and ambiguity evidence."""
    columns = [_classify_column(column, profile.row_count) for column in profile.columns]
    counts = Counter(str(item["primary_role"]) for item in columns)
    ambiguous_columns = [
        str(item["column"]) for item in columns if bool(item["ambiguous"])
    ]
    return {
        "schema": ROLE_SCHEMA,
        "method": "deterministic-profile-heuristics-v1",
        "columns": columns,
        "summary": {
            "time": counts["time"],
            "metric": counts["metric"],
            "dimension": counts["dimension"],
            "identifier": counts["identifier"],
            "unknown": counts["unknown"],
            "ambiguous": len(ambiguous_columns),
        },
        "ambiguous_columns": ambiguous_columns,
        "requires_confirmation": bool(ambiguous_columns),
    }


def infer_data_roles_from_mapping(
    profile: Mapping[str, Any],
    *,
    dataset_ref: str | None = None,
) -> dict[str, Any]:
    """Infer roles from a persisted profile after validating its safe metadata.

    TaskRun preconditions consume the profile already stored beside a Dataset,
    not raw rows and not a model response.  Older or malformed profiles fail
    closed here so callers can require a fresh governed profile instead of
    silently guessing from partial metadata.
    """
    row_count = _required_non_negative_int(profile.get("row_count"), "row_count")
    raw_columns = profile.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("数据画像缺少 columns 元数据")
    columns: list[ColumnProfile] = []
    for index, raw_column in enumerate(raw_columns):
        if not isinstance(raw_column, Mapping):
            raise ValueError(f"数据画像第 {index + 1} 列格式非法")
        name = raw_column.get("name")
        dtype = raw_column.get("dtype")
        null_ratio = raw_column.get("null_ratio")
        distinct_count = raw_column.get("distinct_count")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"数据画像第 {index + 1} 列缺少 name")
        if not isinstance(dtype, str) or not dtype.strip():
            raise ValueError(f"数据画像列 {name} 缺少 dtype")
        if (
            not isinstance(null_ratio, int | float)
            or isinstance(null_ratio, bool)
            or not 0.0 <= float(null_ratio) <= 1.0
        ):
            raise ValueError(f"数据画像列 {name} 的 null_ratio 非法")
        clean_distinct_count = _required_non_negative_int(
            distinct_count,
            f"columns[{index}].distinct_count",
        )
        columns.append(
            ColumnProfile(
                name=name.strip(),
                dtype=dtype.strip(),
                null_ratio=float(null_ratio),
                distinct_count=clean_distinct_count,
            )
        )
    raw_column_count = profile.get("column_count", len(columns))
    column_count = _required_non_negative_int(raw_column_count, "column_count")
    if column_count != len(columns):
        raise ValueError("数据画像 column_count 与 columns 数量不一致")
    raw_ref = dataset_ref if dataset_ref is not None else profile.get("dataset_ref", "")
    clean_ref = raw_ref.strip() if isinstance(raw_ref, str) else ""
    return infer_data_roles(
        DataProfile(
            dataset_ref=clean_ref,
            row_count=row_count,
            column_count=column_count,
            columns=columns,
        )
    )


def diagnose_data_quality(
    profile: DataProfile,
    roles: dict[str, Any],
    *,
    duplicate_rows: int,
) -> dict[str, Any]:
    """Return profile-backed issues and non-mutating cleaning recommendations."""
    role_items = cast(list[dict[str, Any]], roles.get("columns") or [])
    role_by_name = {str(item.get("column")): item for item in role_items}
    issues: list[dict[str, Any]] = []

    if duplicate_rows > 0:
        duplicate_ratio = duplicate_rows / profile.row_count if profile.row_count else 0.0
        _append_issue(
            issues,
            code="duplicate_rows",
            severity="high" if duplicate_ratio >= 0.1 else "medium",
            columns=[],
            evidence={
                "duplicate_rows": duplicate_rows,
                "duplicate_ratio": round(duplicate_ratio, 4),
            },
            action="review_duplicate_rows",
            recommendation="确认重复记录的业务主键和保留规则后再去重，不能直接静默删除。",
        )

    for column in profile.columns:
        role_item = role_by_name.get(column.name, {})
        primary_role = str(role_item.get("primary_role", "unknown"))
        non_null_count = _non_null_count(column, profile.row_count)

        if column.null_ratio >= 1.0:
            _append_issue(
                issues,
                code="all_values_missing",
                severity="high",
                columns=[column.name],
                evidence={"null_ratio": 1.0, "role": primary_role},
                action="restore_or_exclude_column",
                recommendation="回源补齐该字段；无法恢复时，经用户确认后从本次分析中排除。",
            )
        elif column.null_ratio > 0:
            _append_issue(
                issues,
                code="missing_values",
                severity=_missing_severity(column.null_ratio),
                columns=[column.name],
                evidence={
                    "null_ratio": round(column.null_ratio, 4),
                    "role": primary_role,
                },
                action="review_missing_values",
                recommendation=_missing_recommendation(primary_role),
            )

        if column.distinct_count == 1:
            _append_issue(
                issues,
                code="constant_column",
                severity="low",
                columns=[column.name],
                evidence={"distinct_count": 1, "role": primary_role},
                action="exclude_constant_from_analysis",
                recommendation="该字段没有区分度；经用户确认后可从分群、相关和建模输入中排除。",
            )

        if primary_role == "identifier" and non_null_count > column.distinct_count:
            duplicate_values = non_null_count - column.distinct_count
            _append_issue(
                issues,
                code="non_unique_identifier",
                severity="high",
                columns=[column.name],
                evidence={
                    "non_null_count": non_null_count,
                    "distinct_count": column.distinct_count,
                    "duplicate_values_at_least": duplicate_values,
                },
                action="confirm_identifier_key",
                recommendation="核对该字段是否真是业务主键，或是否需要与其他字段组成联合主键。",
            )

        if bool(role_item.get("ambiguous")):
            _append_issue(
                issues,
                code="ambiguous_data_role",
                severity="medium",
                columns=[column.name],
                evidence={
                    "primary_role": primary_role,
                    "confidence": role_item.get("confidence"),
                    "candidate_roles": [
                        candidate.get("role")
                        for candidate in cast(
                            list[dict[str, Any]], role_item.get("candidates") or []
                        )[:3]
                    ],
                },
                action="confirm_data_role",
                recommendation="在聚合、建模或 Join 前由用户确认该字段的数据角色。",
            )

    severity_counts = Counter(str(item["severity"]) for item in issues)
    recommendations = [
        {
            "issue_id": item["issue_id"],
            "action": item["suggested_action"],
            "columns": item["columns"],
            "message": item["recommendation"],
            "automatic": False,
        }
        for item in issues
    ]
    high_null_columns = [
        {"name": column.name, "null_ratio": round(column.null_ratio, 4)}
        for column in profile.columns
        if column.null_ratio >= 0.3
    ]
    return {
        "schema": QUALITY_SCHEMA,
        "mutates_data": False,
        "duplicate_rows": duplicate_rows,
        "high_null_columns": high_null_columns,
        "constant_columns": [
            column.name for column in profile.columns if column.distinct_count == 1
        ],
        "issues": issues,
        "recommendations": recommendations,
        "summary": {
            "issue_count": len(issues),
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "requires_confirmation": bool(issues),
        },
    }


def _classify_column(column: ColumnProfile, row_count: int) -> dict[str, Any]:
    normalized_name = _normalize_name(column.name)
    non_null_count = _non_null_count(column, row_count)
    unique_ratio = (
        column.distinct_count / non_null_count if non_null_count > 0 else 0.0
    )
    time_hint = _contains_hint(normalized_name, _TIME_HINTS)
    identifier_hint = _is_identifier_name(normalized_name)
    metric_hint = _contains_hint(normalized_name, _METRIC_HINTS)
    dimension_hint = _contains_hint(normalized_name, _DIMENSION_HINTS)
    free_text_hint = _contains_hint(normalized_name, _FREE_TEXT_HINTS)
    candidates: dict[DataRole, tuple[float, list[str]]] = {}

    def propose(role: DataRole, score: float, *evidence: str) -> None:
        current = candidates.get(role)
        reasons = list(current[1]) if current else []
        for item in evidence:
            if item not in reasons:
                reasons.append(item)
        candidates[role] = (max(current[0] if current else 0.0, score), reasons)

    if column.distinct_count == 0:
        propose("unknown", 0.99, "cardinality:no_non_null_values")
    elif column.distinct_count == 1:
        propose("unknown", 0.9, "cardinality:constant")
        propose("dimension", 0.45, "cardinality:constant_category")
    else:
        if column.dtype == "datetime":
            propose("time", 0.99 if time_hint else 0.97, "dtype:datetime")
            if time_hint:
                propose("time", 0.99, "name:time_hint")
        elif time_hint:
            propose("time", 0.9, "name:time_hint", f"dtype:{column.dtype}")

        if identifier_hint:
            propose(
                "identifier",
                0.98 if unique_ratio >= 0.95 else 0.84,
                "name:identifier_hint",
                "cardinality:near_unique" if unique_ratio >= 0.95 else "cardinality:not_unique",
            )
        elif unique_ratio >= 0.98 and column.dtype == "str":
            propose("identifier", 0.62, "cardinality:unique_text")

        if column.dtype in _NUMERIC_DTYPES:
            metric_score = 0.95 if metric_hint else 0.82
            if time_hint or identifier_hint or dimension_hint:
                metric_score = 0.55
            propose("metric", metric_score, f"dtype:{column.dtype}")
            if metric_hint:
                propose("metric", metric_score, "name:metric_hint")
            if dimension_hint:
                propose("dimension", 0.95, "name:dimension_hint")
            elif column.distinct_count <= 20 and unique_ratio < 0.5:
                # 无语义提示的低基数数值可能是指标，也可能是编码类维度。
                # 保留 metric 主候选，但缩小候选差距以强制用户确认。
                propose("dimension", 0.76, "cardinality:low_numeric")
        elif column.dtype == "bool":
            propose("dimension", 0.98, "dtype:bool")
        elif column.dtype == "str":
            if free_text_hint:
                propose("unknown", 0.86, "name:free_text_hint")
            elif identifier_hint or time_hint:
                propose("dimension", 0.52, "dtype:str")
            elif dimension_hint or column.distinct_count <= 20:
                propose(
                    "dimension",
                    0.96 if dimension_hint else 0.9,
                    "name:dimension_hint" if dimension_hint else "cardinality:categorical",
                    "dtype:str",
                )
            else:
                propose("dimension", 0.68, "dtype:str", "cardinality:high")
                propose("identifier", 0.6, "cardinality:near_unique_text")

    ranked = sorted(
        candidates.items(),
        key=lambda item: (-item[1][0], item[0]),
    )
    if not ranked:
        ranked = [("unknown", (0.5, ["profile:insufficient_evidence"]))]
    primary_role = ranked[0][0]
    confidence = ranked[0][1][0]
    second_score = ranked[1][1][0] if len(ranked) > 1 else 0.0
    ambiguous = (
        primary_role == "unknown"
        or confidence < 0.75
        or confidence - second_score < 0.1
    )
    return {
        "column": column.name,
        "primary_role": primary_role,
        "confidence": round(confidence, 2),
        "ambiguous": ambiguous,
        "candidates": [
            {
                "role": role,
                "score": round(score, 2),
                "evidence": evidence,
            }
            for role, (score, evidence) in ranked
            if score >= 0.45
        ],
        "profile_evidence": {
            "dtype": column.dtype,
            "null_ratio": round(column.null_ratio, 4),
            "distinct_count": column.distinct_count,
            "non_null_count": non_null_count,
            "unique_ratio": round(unique_ratio, 4),
        },
    }


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    severity: Severity,
    columns: list[str],
    evidence: dict[str, Any],
    action: str,
    recommendation: str,
) -> None:
    issues.append(
        {
            "issue_id": f"quality-{len(issues) + 1:03d}",
            "code": code,
            "severity": severity,
            "columns": columns,
            "evidence": evidence,
            "suggested_action": action,
            "recommendation": recommendation,
        }
    )


def _missing_severity(null_ratio: float) -> Severity:
    if null_ratio >= 0.5:
        return "high"
    if null_ratio >= 0.1:
        return "medium"
    return "low"


def _missing_recommendation(role: str) -> str:
    if role == "time":
        return "回源修复时间值；无法可靠推断时排除相应记录，不得前后填充制造时间。"
    if role == "metric":
        return "先确认缺失机制，再选择删除或受治理填补；不得静默用 0 替代。"
    if role == "dimension":
        return "仅在业务允许时映射为“未知”类别，否则保留缺失并单独披露。"
    if role == "identifier":
        return "回源补齐标识；不得生成伪 ID 来掩盖缺失。"
    return "先确认字段含义和缺失机制，再决定删除、填补或保留。"


def _non_null_count(column: ColumnProfile, row_count: int) -> int:
    return max(0, min(row_count, round(row_count * (1.0 - column.null_ratio))))


def _required_non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"数据画像 {label} 必须是非负整数")
    return value


def _normalize_name(name: str) -> str:
    with_camel_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
    return re.sub(r"[\s\-()（）\[\]【】]+", "_", with_camel_boundaries.casefold())


def _contains_hint(name: str, hints: tuple[str, ...]) -> bool:
    tokens = set(name.split("_"))
    return any(hint in tokens if hint.isascii() else hint in name for hint in hints)


def _is_identifier_name(name: str) -> bool:
    tokens = set(name.split("_"))
    non_ascii_id_suffix = name.endswith("id") and any(
        not character.isascii() for character in name[:-2]
    )
    if name in {"id", "key", "主键"} or "id" in tokens or non_ascii_id_suffix:
        return True
    return _contains_hint(name, _IDENTIFIER_HINTS)
