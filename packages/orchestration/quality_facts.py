"""画像诊断集合的只读投影；数量仍只从 value_index 读取。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

QUALITY_FACT_SCHEMA = "profile-quality-facts-v1"
QUALITY_SOURCE_SCHEMA = "chatbi-data-quality-v1"
QUALITY_COUNT_PATH = "$.quality.summary.issue_count"


def content_hash(value: Any) -> str:
    """为不可变投影与来源计算规范内容摘要。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def quality_projection(result: Any, context: dict[str, Any]) -> dict[str, Any] | None:
    """由实际画像结果投影身份和建议关系，缺数值不根据列表长度补造。"""
    if not isinstance(result, dict) or not isinstance(result.get("profile"), dict):
        return None
    quality = result.get("quality")
    if not isinstance(quality, dict):
        return None
    issues = quality.get("issues")
    recommendations = quality.get("recommendations")
    summary = quality.get("summary")
    columns = result["profile"].get("columns")
    valid = (
        quality.get("schema") == QUALITY_SOURCE_SCHEMA
        and quality.get("mutates_data") is False
        and isinstance(issues, list)
        and isinstance(recommendations, list)
        and isinstance(summary, dict)
        and type(summary.get("issue_count")) is int
        and summary["issue_count"] >= 0
        and type(summary.get("requires_confirmation")) is bool
        and isinstance(columns, list)
        and isinstance(context.get("dataset_ref"), str)
    )
    identities: list[dict[str, Any]] = []
    advice: list[dict[str, Any]] = []
    if valid:
        assert isinstance(issues, list) and isinstance(recommendations, list)
        assert isinstance(summary, dict) and isinstance(columns, list)
        names = [column.get("name") for column in columns if isinstance(column, dict)]
        valid = len(names) == len(columns) and all(isinstance(n, str) for n in names)
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict) or not (
                isinstance(issue.get("issue_id"), str)
                and issue["issue_id"]
                and isinstance(issue.get("code"), str)
                and isinstance(issue.get("columns"), list)
                and all(c in names for c in issue["columns"])
                and isinstance(issue.get("evidence"), dict)
                and isinstance(issue.get("suggested_action"), str)
            ):
                valid = False
                break
            identities.append(
                {
                    "issue_id": issue["issue_id"],
                    "code": issue["code"],
                    "columns": issue["columns"],
                    "action": issue["suggested_action"],
                    "path": f"$.quality.issues[{index}]",
                }
            )
        ids = [issue["issue_id"] for issue in identities]
        valid = valid and len(set(ids)) == len(ids) == len(issues) == summary["issue_count"]
        for recommendation in recommendations:
            if not isinstance(recommendation, dict):
                valid = False
                break
            selected = [i for i in identities if i["issue_id"] == recommendation.get("issue_id")]
            if len(selected) != 1 or not (
                recommendation.get("action") == selected[0]["action"]
                and recommendation.get("columns") == selected[0]["columns"]
                and recommendation.get("automatic") is False
                and isinstance(recommendation.get("message"), str)
            ):
                valid = False
                break
            advice.append(
                {
                    k: recommendation[k]
                    for k in ("issue_id", "action", "columns", "message", "automatic")
                }
            )
        valid = valid and len(advice) == len(ids) and {a["issue_id"] for a in advice} == set(ids)
    projection = {
        "schema": QUALITY_FACT_SCHEMA,
        "source_schema": quality.get("schema"),
        "source_hash": content_hash(result),
        "context": context,
        "count_path": QUALITY_COUNT_PATH,
        "issues": identities,
        "recommendations": advice,
        "columns": [
            c["name"]
            for c in (columns or [])
            if isinstance(c, dict) and isinstance(c.get("name"), str)
        ],
        "requires_confirmation": summary.get("requires_confirmation")
        if isinstance(summary, dict)
        else None,
        "state": "available" if valid else "unavailable",
    }
    return {**projection, "projection_hash": content_hash(projection)}


def valid_quality_projection(summary: dict[str, Any]) -> bool:
    """重核投影、上下文和显式数值的一致性，不接受模型自报集合。"""
    projection = summary.get("quality_projection")
    if not isinstance(projection, dict) or not (
        projection.get("schema") == QUALITY_FACT_SCHEMA
        and projection.get("source_schema") == QUALITY_SOURCE_SCHEMA
        and projection.get("state") == "available"
        and projection.get("context") == summary.get("numeric_context")
        and projection.get("count_path") == QUALITY_COUNT_PATH
        and projection.get("projection_hash")
        == content_hash({k: v for k, v in projection.items() if k != "projection_hash"})
    ):
        return False
    issues = projection.get("issues")
    if not isinstance(issues, list) or not all(isinstance(i, dict) for i in issues):
        return False
    ids = [i.get("issue_id") for i in issues]
    values = [
        v
        for v in summary.get("value_index", [])
        if isinstance(v, dict) and v.get("path") == QUALITY_COUNT_PATH
    ]
    return (
        len(ids) == len(set(ids))
        and len(values) == 1
        and values[0].get("source_kind") == "number"
        and values[0].get("value") == str(len(ids))
        and not summary.get("value_index_truncated")
    )


def seal_quality_facts(
    summary: dict[str, Any], result: Any, context: dict[str, Any]
) -> dict[str, Any]:
    """成功事务只从实际结果封存新质量契约；不依赖 apps 或调用方数量。"""
    projection = quality_projection(result, context)
    values = [v for v in summary.get("value_index", []) if v.get("path") != QUALITY_COUNT_PATH]
    if projection is not None and projection["state"] == "available":
        values.append(
            {
                "path": QUALITY_COUNT_PATH,
                "value": str(result["quality"]["summary"]["issue_count"]),
                "source_kind": "number",
            }
        )
    return {
        **summary,
        "numeric_context": context,
        "value_index": values,
        **({"quality_projection": projection} if projection is not None else {}),
    }
