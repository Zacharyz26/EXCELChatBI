"""统计结果适配与对象解析：先约束完整语义身份，再比较数值。"""

from __future__ import annotations

import re
from typing import Any

from packages.session.models import JsonObject

from apps.orchestrator.control.claim_relations import SubjectRelation, parse_subject_relation
from apps.orchestrator.control.dataset_identity import (
    identity_declarations,
    identity_literal,
    identity_matches_binding,
)
from apps.orchestrator.control.statistic_roles import (
    ROLE_LABELS,
    label_pattern,
    mentioned_roles,
    role_mentions,
)

TOOL_LABELS: dict[str, tuple[str, ...]] = {
    "trend_analysis": ("趋势分析", "趋势工具"),
    "correlation": ("相关性分析", "相关分析"),
    "regression": ("回归分析", "回归模型"),
    "anomaly_detect": ("异常检测",),
    "forecast": ("预测分析", "预测模型", "受治理预测"),
    "dimension_contribution": ("维度贡献", "贡献分析"),
    "group_compare": ("分群比较", "组间比较"),
}
TEST_LABELS: dict[str, tuple[str, ...]] = {
    "residual_normality": ("残差正态性", "正态性检验", "jarque_bera", "Jarque-Bera"),
    "heteroskedasticity": ("异方差检验", "异方差", "breusch_pagan", "Breusch-Pagan"),
    "autocorrelation": ("自相关检验", "durbin_watson", "Durbin-Watson"),
    "multicollinearity": ("多重共线性", "共线性诊断"),
}
COMPONENT_LABELS: dict[str, tuple[str, ...]] = {
    "split": ("训练验证划分",),
    "prediction_interval": ("预测区间",),
    "time_axis": ("时间轴",),
    "small_group_protection": ("小组保护",),
}
METHODS = {
    "pearson",
    "spearman",
    "ols",
    "logit",
    "ma",
    "stl",
    "prophet",
    "iqr",
    "3sigma",
    "isolation_forest",
    "naive",
    "drift",
    "seasonal_naive",
    "welch_t",
    "welch_anova",
}


def _tokens(path: str) -> list[str | int]:
    return [
        int(index) if index else key for key, index in re.findall(r"\.([^.[\]]+)|\[(\d+)\]", path)
    ]


def _at(value: Any, tokens: list[str | int]) -> Any:
    for token in tokens:
        if isinstance(value, dict) and isinstance(token, str):
            value = value.get(token)
        elif isinstance(value, list | tuple) and isinstance(token, int) and token < len(value):
            value = value[token]
        else:
            return None
    return value


def annotate_objects(values: list[JsonObject], result: Any, arguments: JsonObject) -> JsonObject:
    """适配现有七类统计结果；每个数字都有对象，不以数字推断对象。"""
    if not isinstance(result, dict):
        return {}
    wrapped = isinstance(result.get("result"), dict) and "kind" in result
    if wrapped:
        result = result["result"]
    args = {**arguments, **arguments.get("tool_args", {})}
    metadata = result.get("statistical_evidence") or {}
    analysis = {
        "kind": metadata.get("analysis_kind"),
        "method": result.get("selected_method", result.get("method", result.get("kind"))),
        "slope_unit": result.get("slope_unit"),
    }
    for item in values:
        if item.get("source_kind") != "number":
            continue
        parts = _tokens(str(item["path"]))
        if wrapped and parts[:1] == ["result"]:
            parts = parts[1:]
        leaf = parts[-1] if parts else ""
        parent = _at(result, parts[:-1])
        obj: JsonObject = {"kind": "analysis"}
        if len(parts) > 1:
            obj = {"kind": "unresolved", "path": str(item["path"]).rsplit(".", 1)[0]}
        if parts[:2] in [["statistical_evidence", "sample"], ["statistical_evidence", "inference"]]:
            obj = {"kind": "analysis"}
        elif parts and parts[0] == "top_pairs" and isinstance(parent, dict):
            obj = {"kind": "column_pair", "members": sorted([str(parent["a"]), str(parent["b"])])}
        elif len(parts) == 3 and parts[0] == "matrix":
            columns = result.get("columns", args.get("columns", []))
            i, j = parts[1:]
            if isinstance(i, int) and isinstance(j, int) and max(i, j) < len(columns):
                obj = {"kind": "column_pair", "members": sorted([str(columns[i]), str(columns[j])])}
                item["statistic"] = "corr"
        elif parts and parts[0] == "coefficients" and isinstance(parent, dict):
            obj = {"kind": "coefficient", "feature": str(parent.get("name"))}
            if parent.get("name") == "const" and leaf == "coef":
                item["statistic"] = "intercept"
        elif len(parts) > 1 and parts[0] == "diagnostics":
            diagnostic = _at(result, parts[:2])
            obj = {
                "kind": "diagnostic",
                "subject": parts[1],
                "test": diagnostic.get("test") if isinstance(diagnostic, dict) else None,
            }
            if isinstance(parent, dict) and "name" in parent:
                obj["feature"] = str(parent["name"])
        elif parts and parts[0] == "pairwise" and isinstance(parent, dict):
            members = [str(parent["left"]), str(parent["right"])]
            ordered = leaf not in {"p_value", "adjusted_p_value"}
            obj = {
                "kind": "group_pair",
                "members": members if ordered else sorted(members),
                "ordered": ordered,
                "test": "welch_t",
            }
        elif parts and parts[0] == "overall":
            obj = {"kind": "overall", "test": result.get("overall", {}).get("test")}
        elif parts and parts[0] in {"groups", "rows"} and isinstance(parent, dict):
            group = parent.get("group", parent.get("dimension"))
            obj = {"kind": "group", "group": str(group)} if group is not None else obj
            if group is not None:
                item["group_key"] = str(group)
        elif parts and parts[0] == "anomalies" and isinstance(parent, dict):
            obj = {
                "kind": "observation",
                "index": str(parent.get("index")),
                "time": parent.get("time"),
            }
        elif parts and parts[0] == "predictions" and isinstance(parent, dict):
            obj = {"kind": "prediction", "time": parent.get("time")}
        elif len(parts) == 3 and parts[0] == "points" and isinstance(parts[2], int):
            times = result.get("time", [])
            obj = {
                "kind": "observation",
                "index": str(parts[2]),
                "time": times[parts[2]] if parts[2] < len(times) else None,
            }
            item["statistic"] = parts[1]
        elif len(parts) == 2 and parts[0] == "forecast" and isinstance(parts[1], int):
            times = result.get("forecast_time", [])
            obj = {"kind": "prediction", "time": times[parts[1]] if parts[1] < len(times) else None}
            item["statistic"] = "point"
        elif parts and parts[0] == "candidate_metrics" and len(parts) > 1:
            obj = {"kind": "forecast_model", "model": parts[1], "window": "validation"}
        elif parts and parts[0] == "validation_metrics":
            obj = {
                "kind": "forecast_model",
                "model": result.get("selected_method"),
                "window": "validation",
            }
        elif parts[:2] == ["baseline", "metrics"]:
            obj = {
                "kind": "forecast_model",
                "model": result.get("baseline", {}).get("method"),
                "window": "validation",
            }
        elif parts and parts[0] in {
            "time_axis",
            "split",
            "prediction_interval",
            "small_group_protection",
        }:
            obj = {"kind": "component", "component": parts[0]}
        item["object"] = obj
    return analysis


def analysis_identity(
    context: JsonObject, source_tool: str | None, summary: JsonObject
) -> JsonObject:
    """数据版本、工具、字段角色及全部分析参数共同定义一次统计分析。"""
    analysis = summary.get("analysis_context") or {}
    fields = {
        "value": context.get("value_col", context.get("y_col", context.get("y"))),
        "time": context.get("time_col"),
        "target": context.get("target"),
        "features": sorted(map(str, context.get("features", []))),
        "columns": sorted(map(str, context.get("columns", []))),
        "dimension": context.get("group_col", context.get("dimension_col", context.get("x"))),
    }
    parameters = {
        key: val
        for key, val in context.items()
        if key
        not in {
            "analysis_id",
            "tool_args",
            "dataset_ref",
            "data_version_hash",
            "encoding",
            "title",
            "columns",
            "features",
            "target",
            "time_col",
            "value_col",
            "group_col",
            "dimension_col",
        }
    }
    return {
        "tool": source_tool,
        "kind": analysis.get("kind"),
        "fields": fields,
        "method": analysis.get("method") or context.get("method", context.get("kind")),
        "parameters": parameters,
    }


def _has(text: str, label: str) -> bool:
    return bool(re.search(label_pattern(label), text, re.I))


def _named(text: str, vocabulary: dict[str, tuple[str, ...]]) -> set[str]:
    found = [
        (key, label)
        for key, labels in vocabulary.items()
        for label in (key, *labels)
        if _has(text, label)
    ]
    return {
        key
        for key, label in found
        if not any(label != other and label in other for _, other in found)
    }


def identity_matches(
    binding: JsonObject,
    text: str,
    universe: list[JsonObject],
    table_cells: tuple[tuple[str, str], ...] | None = None,
) -> bool:
    """共享对象约束；计数和参数与业务统计量使用同一来源、模型及主体规则。"""
    analysis = binding.get("analysis") or {}
    tool = analysis.get("tool")
    named_tools = _named(text, TOOL_LABELS)
    if named_tools and named_tools != {tool}:
        return False
    declarations = [m for m in identity_declarations(text) if identity_literal(m["value"])]
    if any(not identity_matches_binding(m, binding) for m in declarations):
        return False
    if tool not in TOOL_LABELS:
        return not named_tools
    obj = binding.get("object") or {"kind": "unresolved"}
    fields = analysis.get("fields") or {}
    all_fields: set[str] = set()
    for entry in universe:
        field_map = (entry.get("analysis") or {}).get("fields") or {}
        for field in field_map.values():
            all_fields.update(
                map(str, field if isinstance(field, list) else [field] if field else [])
            )
        feature = (entry.get("object") or {}).get("feature")
        if feature:
            all_fields.add(str(feature))
    mentioned = {field for field in all_fields if _has(text, field)}
    mentioned = {
        field
        for field in mentioned
        if not any(field != other and field in other for other in mentioned)
    }
    actual_fields = {
        str(value) for value in fields.values() if value and not isinstance(value, list)
    }
    actual_fields.update(fields.get("columns", []))
    actual_fields.update(fields.get("features", []))
    if not mentioned.issubset(actual_fields | {str(obj.get("feature"))}):
        return False
    methods = {method for method in METHODS if _has(text, method)}
    methods = {
        method
        for method in methods
        if not any(method != other and method in other for other in methods)
    }
    effective_method = obj.get("model", obj.get("test") or analysis.get("method"))
    allowed_methods = {obj.get("model") or analysis.get("method")}
    if obj.get("test") in {"welch_t", "welch_anova"}:
        allowed_methods = {obj["test"]}
    if methods and not methods.issubset(allowed_methods):
        return False
    named_tests = _named(text, TEST_LABELS)
    if named_tests and (obj.get("kind") != "diagnostic" or named_tests != {obj.get("subject")}):
        return False
    named_components = _named(text, COMPONENT_LABELS)
    if named_components and named_components != {obj.get("component")}:
        return False
    # A target named as a model is directed, unlike a symmetric correlation pair.
    targets = re.findall(r"([A-Za-z\u4e00-\u9fff]+)模型", text)
    if targets and any(
        target not in {fields.get("target"), "回归", "预测", "所选", "候选", "基线"}
        for target in targets
    ):
        return False
    kind = obj.get("kind")
    relation: SubjectRelation | None
    table_subjects: list[tuple[str, str]] | None = None
    if table_cells is not None:
        table_subjects = []
        for header, cell in table_cells:
            key = header.casefold()
            if mentioned_roles(header, set(ROLE_LABELS)):
                continue  # Other measurements receive their own fragments.
            if key in {"分析", "工具", "tool"}:
                if cell.casefold() not in {
                    str(tool).casefold(),
                    *(label.casefold() for label in TOOL_LABELS[tool]),
                }:
                    return False
            elif key in {"方法", "method"}:
                if cell.casefold() not in {str(method).casefold() for method in allowed_methods}:
                    return False
            elif key in {"数据集", "dataset_ref", "版本", "dataset_version"}:
                if cell not in {binding.get("dataset_ref"), binding.get("dataset_version")}:
                    return False
            elif key in {"指标", "metric"} and kind == "group_pair":
                if cell != fields.get("value"):
                    return False
            else:
                table_subjects.append((header, cell))
    qualifiers = tuple(
        word for key, labels in TOOL_LABELS.items() for word in (key, *labels)
    ) + tuple(METHODS)
    qualifiers += ("结果", "本次", "本轮")
    if kind in {"column_pair", "group_pair"}:
        members = list(map(str, obj.get("members", [])))
        if table_subjects is not None:
            if len(table_subjects) == 2:
                relation = SubjectRelation(tuple(cell for _, cell in table_subjects), True)
            elif len(table_subjects) == 1 and table_subjects[0][0].casefold() in {
                "列对",
                "变量对",
                "分组对",
                "pair",
            }:
                relation = parse_subject_relation(
                    table_subjects[0][1], str(binding.get("statistic")), (), subject_only=True
                )
                if relation is None or not relation.explicit:
                    return False
            else:
                return False
        else:
            relation = parse_subject_relation(
                text,
                str(binding.get("statistic")),
                qualifiers,
                str(fields["value"]) if kind == "group_pair" and fields.get("value") else None,
            )
        if relation is None:
            return False
        if relation.explicit:
            # Sorting retains multiplicity: (Sales, Sales) is not (Sales, Profit).
            if obj.get("ordered"):
                if list(relation.members) != members:
                    return False
            elif sorted(relation.members) != sorted(members):
                return False
            if kind == "column_pair" and relation.directed:
                return False
        elif not (
            table_subjects is None
            and kind == "column_pair"
            and len(relation.members) == 1
            and relation.members[0] in members
            and len(set(members)) == 2
            and sorted(fields.get("columns", [])) == sorted(members)
        ):
            # Only a selected two-column analysis can justify a one-member
            # prose abbreviation; global candidate identity must also be unique.
            return False
    elif kind == "diagnostic":
        if not named_tests:
            return False
        if obj.get("feature") and not _has(text, str(obj["feature"])):
            return False
    elif kind == "coefficient":
        feature = str(obj.get("feature"))
        if not _has(text, feature) and not (feature == "const" and "截距" in text):
            return False
        features = set(fields.get("features", [])) | {"const"}
        if (mentioned & features) - {feature}:
            return False
        if table_subjects is not None:
            feature_seen = False
            for header, cell in table_subjects:
                if header.casefold() in {"自变量", "特征", "变量", "feature", "predictor"}:
                    if cell != feature:
                        return False
                    feature_seen = True
                elif header.casefold() in {"因变量", "目标", "模型", "target", "response"}:
                    if cell != fields.get("target"):
                        return False
                else:
                    return False
            if not feature_seen:
                return False
        else:
            relation = parse_subject_relation(text, str(binding.get("statistic")), qualifiers)
            if relation is None:
                return False
            if relation.explicit:
                if not relation.directed or relation.members != (feature, fields.get("target")):
                    return False
            else:
                subject = relation.members[0] if relation.members else ""
                target = str(fields.get("target"))
                if subject not in {feature, target + "模型" + feature, target + "模型的" + feature}:
                    if not (
                        feature == "const"
                        and not subject
                        and binding.get("statistic") == "intercept"
                    ):
                        return False
    elif kind == "forecast_model":
        if "基线" in text and obj.get("model") != "naive":
            return False
        if "所选" in text and obj.get("model") != analysis.get("method"):
            return False
    elif kind in {"observation", "prediction"}:
        time = str(obj.get("time") or "")
        index = str(obj.get("index") or "")
        if not (
            (time and time in text)
            or (index and re.search(r"索引\s*[:：=]?\s*" + re.escape(index) + r"(?!\d)", text))
        ):
            return False
    elif kind == "overall":
        if not (
            "总体" in text
            or "整体" in text
            or (effective_method and _has(text, str(effective_method)))
        ):
            return False
    elif kind == "unresolved":
        return False
    if binding.get("unit") == "count":
        starts = [
            start
            for start, _, role in role_mentions(text, {str(binding.get("statistic"))})
            if role == binding.get("statistic")
        ]
        if starts:
            prefix = text[: min(starts)]
            for label in sorted(
                all_fields
                | METHODS
                | {
                    word
                    for vocabulary in (TOOL_LABELS, COMPONENT_LABELS)
                    for key, words in vocabulary.items()
                    for word in (key, *words)
                },
                key=len,
                reverse=True,
            ):
                prefix = re.sub(label_pattern(label), "", prefix, flags=re.I)
            prefix = re.sub(r"(?:dataset_ref|数据集|版本)\s*[:：=]?\s*[a-f0-9]{32,64}", "", prefix)
            prefix = re.sub(
                r"报告内容|根据|结果|说明|分析|本次|本轮|当前|有效|原始|的|为|是|共|中", "", prefix
            )
            if re.search(r"[A-Za-z\u4e00-\u9fff]", prefix):
                return False
    unit = binding.get("unit")
    if unit == "count" and re.search(r"\d\s*(?:%|元|万元|亿元|天)", text):
        return False
    if unit == "ratio" and any(word in text for word in ("元", "万元", "亿元", "天", "行")):
        return False
    if unit == "value_per_day" and any(word in text for word in ("每月", "每年", "每小时")):
        return False
    return True
