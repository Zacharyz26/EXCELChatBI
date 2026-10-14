"""Deterministic Claim extraction and Evidence linking.

Stage 1 deliberately keeps this extractor conservative: numeric claims require
a value present in current-run Evidence, allowing only deterministic display
rounding and explicit units such as ``%``/``万``/``亿``. Knowledge claims require
an explicit source label returned by a governed knowledge Tool. It does not ask
a model to invent links between prose and Evidence.
"""

from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from packages.common.exact_numbers import decode_exact_number
from packages.session.models import JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord

from apps.orchestrator.control.claim_structure import dimension_mentions, table_claim_fragments
from apps.orchestrator.control.semantic_identity import (
    TOOL_LABELS,
    analysis_identity,
    annotate_objects,
    identity_matches,
)
from apps.orchestrator.control.statistic_roles import (
    ROLE_LABELS,
    STATISTICAL_TOOLS,
    mentioned_roles,
    statistic_role,
    statistic_scope,
)

_NUMBER_PATTERN = re.compile(
    # Treat only ASCII identifier characters as token boundaries. Chinese unit
    # expressions commonly omit spaces (``10万元``/``366行``) and must still be
    # auditable numeric claims.
    r"(?<![A-Za-z0-9_.\-])"
    r"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?%?(?![A-Za-z0-9_.\-])"
)
_LEADING_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s+|\d+[.)、]\s*)")
_MAX_EVIDENCE_VALUES = 256
_MAX_EVIDENCE_SOURCES = 64
_MAX_VALUE_DEPTH = 8
_DISPLAY_UNIT_FACTORS: dict[str, Decimal] = {
    "千": Decimal("1000"),
    "万": Decimal("10000"),
    "亿": Decimal("100000000"),
}
_NO_KNOWLEDGE_RESULT_PATTERN = re.compile(
    r"(?:未找到|没有找到|没查到|未检索到|没有检索到|没能检索到|无相关|没有相关|检索无结果|"
    r"无法(?:依据|从).{0,12}(?:回答|确认)|"
    r"知识库.{0,8}(?:没有|无|未收录))"
)
_LIMITATION_PATTERN = re.compile(
    r"(?:局限|限制|注意|仅供参考|不能代表|不代表因果|样本不足|缺少|不确定)"
)
_PATH_ALIASES: dict[str, tuple[str, ...]] = {
    "row_count": ("总行数", "数据行", "记录数", "样本数", "行数"),
    "column_count": ("总列数", "字段数", "列数"),
    "duplicate_rows": ("重复记录", "重复行", "重复"),
    "duplicate_ratio": ("重复率",),
    "null_count": ("空值", "缺失值", "缺失"),
    "null_ratio": ("空值率", "缺失率"),
    "p_value": ("p值", "p 值", "显著性"),
    "n_anomalies": ("异常数", "异常点", "异常记录"),
    "group_total": ("分组数", "组数", "月份数"),
    "valid_value_count": ("有效值数", "有效记录"),
}


def build_evidence_summary(
    *,
    summary: str,
    result: Any,
    artifact_id: str | None,
    arguments: JsonObject | None = None,
) -> JsonObject:
    """Create bounded value/source indexes without copying the whole result."""
    values: list[JsonObject] = []
    sources: list[JsonObject] = []
    if isinstance(result, dict) and isinstance(result.get("option"), dict):
        _collect_chart_values(result["option"], values, arguments or {})
    else:
        _collect_values(result, "$", values, depth=0)
    _collect_sources(result, "$", sources, depth=0)
    report_validation = result.get("validation") if isinstance(result, dict) else None
    report_proof: JsonObject = {}
    if (
        isinstance(report_validation, dict)
        and report_validation.get("validator") == "selected-artifact-numeric-v1"
    ):
        from apps.orchestrator.control.report_delivery import validation_hash

        values = []
        for index, delivery in enumerate(report_validation.get("total_deliveries", [])):
            items: list[JsonObject] = []
            _collect_values(
                delivery["value"], f"$.validation.total_deliveries[{index}].value", items, depth=0
            )
            for item in items:
                item["report_binding"] = delivery["semantic_binding"]
                values.append(item)
        report_proof["report_validation_hash"] = validation_hash(report_validation)
    analysis_context = annotate_objects(values, result, arguments or {})
    return {
        "summary": summary,
        "numeric_context": dict(arguments or {}),
        "analysis_context": analysis_context,
        "artifact_id": artifact_id,
        "value_index": values,
        "value_index_truncated": len(values) >= _MAX_EVIDENCE_VALUES,
        "source_index": sources,
        "source_index_truncated": len(sources) >= _MAX_EVIDENCE_SOURCES,
        "knowledge_empty": _knowledge_result_is_empty(result),
        **report_proof,
    }


def extract_claims(
    *, final_text: str, goal: str, evidence: list[EvidenceRecord]
) -> list[ClaimDraft]:
    """Extract every deterministic Stage-1 Claim type from a candidate answer."""
    claims = extract_numeric_claims(
        final_text=final_text,
        goal=goal,
        evidence=evidence,
    )
    claims.extend(extract_knowledge_claims(final_text=final_text, evidence=evidence))
    claims.extend(extract_limitation_claims(final_text=final_text))
    return claims


def extract_numeric_claims(
    *,
    final_text: str,
    goal: str,
    evidence: list[EvidenceRecord],
    evidence_is_typed: bool = True,
) -> list[ClaimDraft]:
    """Extract numeric statements and link each number to current-run Evidence.

    Typed-only evidence contains raw measurements, not preformatted display
    strings: explicit units must be applied before any numeric match.
    """
    goal_numbers = {
        normalized
        for token in _numeric_tokens(goal)
        if (normalized := _normalize_number(token)) is not None
    }
    evidence_values = _evidence_values(evidence)
    evidence_by_id = {item.evidence_id: item for item in evidence}
    dimensions: dict[str, set[str]] = {}
    for record in evidence:
        for item in record.summary.get("value_index", []):
            binding = _semantic_binding(record, str(item.get("path")))
            if binding.get("grain") and binding.get("group_key") is not None:
                dimensions.setdefault(str(binding["grain"]), set()).add(str(binding["group_key"]))
            obj = binding.get("object") or {}
            if obj.get("kind") == "group_pair" and binding.get("grain"):
                dimensions.setdefault(str(binding["grain"]), set()).update(obj.get("members", []))
            if obj.get("index") is not None:
                dimensions.setdefault("索引", set()).add(str(obj["index"]))
            if obj.get("time"):
                dimensions.setdefault("时间", set()).add(str(obj["time"]))
    claims: list[ClaimDraft] = []
    statements = [
        (statement, fragment.cells, fragment.value_span)
        for fragment in table_claim_fragments(final_text, set(dimensions))
        for statement in (
            [fragment.text] if fragment.cells is not None else _split_statements(fragment.text)
        )
    ]
    for raw_statement, table_cells, value_span in statements:
        statement = (
            raw_statement
            if table_cells is not None
            else _LEADING_LIST_MARKER.sub("", raw_statement)
        ).strip()
        if not statement:
            continue
        refs: list[JsonObject] = []
        linked_ids: list[str] = []
        labels = dimension_mentions(statement, dimensions)
        for match in _NUMBER_PATTERN.finditer(statement):
            if value_span is not None and not (
                value_span[0] <= match.start() < match.end() <= value_span[1]
            ):
                continue
            if any(start <= match.start() and match.end() <= end for start, end, _, _ in labels):
                continue
            token = match.group(0)
            normalized = _normalize_number(token)
            if normalized is None or (
                normalized in goal_numbers
                and _looks_like_time_scope(statement, match.start(), match.end())
            ):
                continue
            semantic_candidates = [
                value
                for value in evidence_values
                if _semantic_candidate_matches(
                    value, statement, match, evidence, dimensions, table_cells, value_span
                )
            ]
            # Resolve statistical objects before looking at the claimed number.
            # Different numbers must not disambiguate different analyses/subjects.
            statistical_identities = {
                json.dumps(
                    {
                        key: val
                        for key, val in _semantic_binding(evidence_by_id[ident], path).items()
                        if key != "evidence_path"
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for ident, path, _, _ in semantic_candidates
                if evidence_by_id[ident].source.get("tool") in TOOL_LABELS
                or evidence_by_id[ident].kind == "stats"
            }
            if len(statistical_identities) > 1:
                semantic_candidates = []
            elif statistical_identities and len({value[2] for value in semantic_candidates}) > 1:
                # Repeated paths may express the same fact, but conflicting values
                # for one immutable statistical identity cannot prove each other.
                semantic_candidates = []
            matched, candidates, match_kind, display_factor = _match_evidence_value(
                normalized,
                semantic_candidates,
                statement=statement,
                token=token,
                token_end=match.end(),
                evidence_is_typed=evidence_is_typed,
            )
            identities = {
                json.dumps(
                    {
                        key: val
                        for key, val in _semantic_binding(evidence_by_id[ident], path).items()
                        if key != "evidence_path"
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for ident, path, _ in candidates
            }
            if len(identities) > 1:
                matched = None
            if matched is None:
                refs.append(
                    {
                        "token": token,
                        "normalized": str(normalized),
                        "supported": False,
                    }
                )
                continue
            evidence_id, path, evidence_value = matched
            value_ref: JsonObject = {
                "token": token,
                "normalized": str(normalized),
                "supported": True,
                "evidence_id": evidence_id,
                "path": path,
                "evidence_value": evidence_value,
                "match_kind": match_kind,
                "semantic_binding": _semantic_binding(evidence_by_id[evidence_id], path),
            }
            if display_factor != Decimal(1):
                value_ref["display_factor"] = str(display_factor)
            if len(candidates) > 1:
                value_ref["candidate_paths"] = [
                    {"evidence_id": item[0], "path": item[1]} for item in candidates
                ]
                value_ref["ambiguous_value"] = True
            refs.append(value_ref)
            for candidate_id, _candidate_path, _candidate_value in candidates:
                if candidate_id not in linked_ids:
                    linked_ids.append(candidate_id)
        if refs:
            definition_refs, definition_evidence_ids = _definition_lineage_refs(
                linked_ids,
                evidence_by_id,
            )
            refs.extend(definition_refs)
            for definition_evidence_id in definition_evidence_ids:
                if definition_evidence_id not in linked_ids:
                    linked_ids.append(definition_evidence_id)
            claims.append(
                ClaimDraft(
                    statement=statement,
                    claim_kind="numeric",
                    value_refs=tuple(refs),
                    evidence_ids=tuple(linked_ids),
                )
            )
    return claims


def _definition_lineage_refs(
    linked_evidence_ids: list[str],
    evidence_by_id: dict[str, EvidenceRecord],
) -> tuple[list[JsonObject], list[str]]:
    refs: list[JsonObject] = []
    definition_evidence_ids: list[str] = []
    for data_evidence_id in linked_evidence_ids:
        data_evidence = evidence_by_id.get(data_evidence_id)
        if data_evidence is None:
            continue
        binding = data_evidence.source.get("definition_execution")
        if not isinstance(binding, dict):
            continue
        definition_evidence_id = binding.get("definition_evidence_id")
        definition_evidence = (
            evidence_by_id.get(definition_evidence_id)
            if isinstance(definition_evidence_id, str)
            else None
        )
        resource = (
            definition_evidence.source.get("definition_resource")
            if definition_evidence is not None
            else None
        )
        supported = isinstance(resource, dict) and all(
            resource.get(resource_key) == binding.get(binding_key)
            for resource_key, binding_key in (
                ("definition_id", "definition_id"),
                ("definition_version", "definition_version"),
                ("semantic_key", "semantic_key"),
                ("formula_hash", "formula_hash"),
                ("resource_uri", "resource_uri"),
                ("source_ref", "source_ref"),
            )
        )
        ref: JsonObject = {
            "kind": "definition_execution",
            "supported": supported,
            "data_evidence_id": data_evidence_id,
            "definition_id": binding.get("definition_id"),
            "definition_version": binding.get("definition_version"),
            "semantic_key": binding.get("semantic_key"),
            "formula_hash": binding.get("formula_hash"),
            "resource_uri": binding.get("resource_uri"),
            "source_ref": binding.get("source_ref"),
        }
        if isinstance(definition_evidence_id, str):
            ref["evidence_id"] = definition_evidence_id
            if supported and definition_evidence_id not in definition_evidence_ids:
                definition_evidence_ids.append(definition_evidence_id)
        if not supported:
            ref["reason"] = "definition_evidence_mismatch"
        refs.append(ref)
    return refs, definition_evidence_ids


def extract_knowledge_claims(
    *, final_text: str, evidence: list[EvidenceRecord]
) -> list[ClaimDraft]:
    """Link knowledge prose to explicit current-run governed knowledge sources.

    The whole answer is one conservative knowledge Claim.  This avoids claiming
    sentence-level semantic alignment that Stage 1 cannot establish reliably,
    while still preventing an uncited or fabricated knowledge answer from being
    delivered.
    """
    knowledge_tools = {"kb_search", "domain_definition_lookup"}
    knowledge = [record for record in evidence if record.source.get("tool") in knowledge_tools]
    if not knowledge or not final_text.strip():
        return []

    source_candidates: list[tuple[str, str, str, str | None]] = []
    for record in knowledge:
        raw_index = record.summary.get("source_index")
        if not isinstance(raw_index, list):
            continue
        for raw in raw_index:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source")
            path = raw.get("path")
            section = raw.get("section")
            if isinstance(source, str) and source.strip() and isinstance(path, str):
                source_candidates.append(
                    (
                        record.evidence_id,
                        path,
                        source.strip(),
                        section.strip() if isinstance(section, str) and section.strip() else None,
                    )
                )

    refs: list[JsonObject] = []
    linked_ids: list[str] = []
    lowered = final_text.casefold()
    for evidence_id, path, source, section in source_candidates:
        if source.casefold() not in lowered:
            continue
        ref: JsonObject = {
            "kind": "knowledge_source",
            "source": source,
            "path": path,
            "supported": True,
            "evidence_id": evidence_id,
            "confidence": "explicit_source_match",
        }
        if section is not None:
            ref["section"] = section
        refs.append(ref)
        if evidence_id not in linked_ids:
            linked_ids.append(evidence_id)

    if source_candidates and not refs:
        refs.append(
            {
                "kind": "knowledge_source",
                "supported": False,
                "reason": "missing_source_citation",
                "available_sources": sorted({item[2] for item in source_candidates}),
            }
        )
    elif not source_candidates:
        honest_no_result = (
            all(record.summary.get("knowledge_empty") is True for record in knowledge)
            and _NO_KNOWLEDGE_RESULT_PATTERN.search(final_text) is not None
        )
        refs.append(
            {
                "kind": "knowledge_no_result",
                "supported": honest_no_result,
                "reason": (
                    "knowledge_search_empty"
                    if honest_no_result
                    else "affirmative_claim_without_knowledge_source"
                ),
                "evidence_id": knowledge[0].evidence_id,
                "confidence": "explicit_no_result" if honest_no_result else "unsupported",
            }
        )
        linked_ids.append(knowledge[0].evidence_id)

    return [
        ClaimDraft(
            statement=final_text.strip()[:4000],
            claim_kind="knowledge",
            value_refs=tuple(refs),
            evidence_ids=tuple(linked_ids),
        )
    ]


def extract_limitation_claims(*, final_text: str) -> list[ClaimDraft]:
    """Record explicit limitations for later semantic verification/audit."""
    claims: list[ClaimDraft] = []
    for raw_statement in _split_statements(final_text):
        statement = _LEADING_LIST_MARKER.sub("", raw_statement).strip()
        if statement and _LIMITATION_PATTERN.search(statement):
            claims.append(
                ClaimDraft(
                    statement=statement,
                    claim_kind="limitation",
                    value_refs=(
                        {
                            "kind": "claim_metadata",
                            "confidence": "explicitly_qualified",
                        },
                    ),
                    evidence_ids=(),
                )
            )
    return claims


def repair_candidate_with_evidence(
    *,
    final_text: str,
    claims: list[ClaimDraft],
    evidence: list[EvidenceRecord],
) -> tuple[str, tuple[str, ...]]:
    """确定性移除无依据数字，并为知识回答补上真实来源标签。

    该修复不会换算或生成新结论：数字句只能保留原文中已被 Evidence 支持的部分；
    来源只能取当前 run 的 ``source_index``。修复后仍必须重新抽取 Claim 并过 Verifier。
    """
    repaired = final_text.strip()
    actions: list[str] = []
    unsupported_numeric = {
        claim.statement
        for claim in claims
        if claim.claim_kind == "numeric"
        and any(ref.get("supported") is not True for ref in claim.value_refs)
    }
    if unsupported_numeric:
        kept = [
            raw
            for raw in _split_statements(repaired)
            if _LEADING_LIST_MARKER.sub("", raw).strip() not in unsupported_numeric
        ]
        repaired = "".join(kept).strip()
        actions.append("removed_unsupported_numeric_statements")
        if not repaired:
            repaired = _evidence_summary_fallback(evidence)
        limitation = "已省略无法由当前工具证据直接支持的派生数字。"
        if limitation not in repaired:
            repaired = f"{repaired}\n\n{limitation}".strip()

    unsupported_knowledge = any(
        claim.claim_kind == "knowledge"
        and any(ref.get("supported") is not True for ref in claim.value_refs)
        for claim in claims
    )
    if unsupported_knowledge:
        sources = _evidence_source_labels(evidence)
        if sources and not any(source.casefold() in repaired.casefold() for source in sources):
            repaired = f"{repaired}\n\n来源：{'；'.join(sources)}".strip()
            actions.append("appended_evidence_sources")

    return repaired, tuple(actions)


def _collect_chart_values(
    option: JsonObject, output: list[JsonObject], args: JsonObject
) -> None:
    """只索引图表数据，并在原始 Evidence 路径保留类目/度量对应关系。"""
    context = {**args, **args.get("tool_args", {})}
    encoding = context.get("encoding") or {}
    for series_index, series in enumerate(option.get("series", [])):
        if not isinstance(series, dict):
            continue
        metric = encoding.get("y") or context.get("value_col") or series.get("name")
        axis = option.get("xAxis") or {}
        if isinstance(axis, list):
            axis_index = series.get("xAxisIndex", 0)
            axis = (
                axis[axis_index]
                if isinstance(axis_index, int) and 0 <= axis_index < len(axis)
                else {}
            )
        categories = axis.get("data", []) if isinstance(axis, dict) else []
        data = series.get("data", [])
        if not isinstance(data, list):
            continue
        for index, point in enumerate(data):
            path = f"$.option.series[{series_index}].data[{index}]"
            group = categories[index] if index < len(categories) else None
            if series.get("type") == "pie":
                group = point.get("name") if isinstance(point, dict) else None
            if isinstance(point, dict):
                point = point.get("value")
                path += ".value"
            if isinstance(point, list):
                # Scatter coordinates are separate metrics; do not index style numbers.
                for coordinate, field in enumerate((encoding.get("x"), metric)):
                    if coordinate < len(point):
                        _collect_values(
                            point[coordinate],
                            f"{path}[{coordinate}]",
                            output,
                            depth=0,
                            metric=field,
                        )
                continue
            _collect_values(
                point,
                path,
                output,
                depth=0,
                group_key=str(group) if group is not None else None,
                metric=metric,
            )


def _collect_values(
    value: Any,
    path: str,
    output: list[JsonObject],
    *,
    depth: int,
    group_key: str | None = None,
    metric: str | None = None,
) -> None:
    if len(output) >= _MAX_EVIDENCE_VALUES or depth > _MAX_VALUE_DEPTH:
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float | Decimal):
        normalized = _decimal_from_value(value)
        if normalized is not None:
            output.append(
                {
                    "path": path,
                    "value": str(normalized),
                    "source_kind": "number",
                    "group_key": group_key,
                    "metric": metric,
                }
            )
        return
    if isinstance(value, str):
        for index, token in enumerate(_numeric_tokens(value)):
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return
            normalized = _normalize_number(token)
            if normalized is not None:
                output.append(
                    {
                        "path": f"{path}#number[{index}]",
                        "value": str(normalized),
                        "source_kind": "text",
                    }
                )
        return
    if isinstance(value, dict):
        exact = decode_exact_number(value)
        if exact is not None:
            output.append(
                {
                    "path": path,
                    "value": str(exact),
                    "source_kind": "number",
                    "group_key": group_key,
                    "metric": metric,
                }
            )
            return
        group_key = str(value["group"]) if value.get("group") is not None else group_key
        metric = str(value["name"]) if value.get("name") is not None else metric
        for key, child in value.items():
            if key == "group":
                continue
            _collect_values(
                child, f"{path}.{key}", output, depth=depth + 1, group_key=group_key, metric=metric
            )
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_values(
                child,
                f"{path}[{index}]",
                output,
                depth=depth + 1,
                group_key=group_key,
                metric=metric,
            )
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return


def _collect_sources(value: Any, path: str, output: list[JsonObject], *, depth: int) -> None:
    if len(output) >= _MAX_EVIDENCE_SOURCES or depth > _MAX_VALUE_DEPTH:
        return
    if isinstance(value, dict):
        source = value.get("source")
        content = value.get("text")
        if not isinstance(content, str):
            content = value.get("snippet")
        if not isinstance(content, str):
            content = value.get("description")
        if isinstance(source, str) and source.strip() and isinstance(content, str):
            item: JsonObject = {"path": path, "source": source.strip()}
            section = value.get("section")
            if isinstance(section, str) and section.strip():
                item["section"] = section.strip()
            output.append(item)
        for key, child in value.items():
            _collect_sources(child, f"{path}.{key}", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_SOURCES:
                return
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_sources(child, f"{path}[{index}]", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_SOURCES:
                return


def _knowledge_result_is_empty(result: Any) -> bool | None:
    if not isinstance(result, dict) or not isinstance(result.get("is_empty"), bool):
        return None
    return bool(result["is_empty"])


def _evidence_source_labels(evidence: list[EvidenceRecord]) -> list[str]:
    sources: list[str] = []
    for record in evidence:
        raw_index = record.summary.get("source_index")
        if not isinstance(raw_index, list):
            continue
        for raw in raw_index:
            source = raw.get("source") if isinstance(raw, dict) else None
            if isinstance(source, str) and source.strip() and source.strip() not in sources:
                sources.append(source.strip())
    return sources[:8]


def _evidence_summary_fallback(evidence: list[EvidenceRecord]) -> str:
    summaries: list[str] = []
    for record in evidence:
        if record.source.get("tool") in {"kb_search", "domain_definition_lookup"}:
            continue
        summary = record.summary.get("summary")
        if isinstance(summary, str) and summary.strip() and summary.strip() not in summaries:
            summaries.append(summary.strip())
    if summaries:
        return "；".join(summaries) + "。"
    return "已完成可验证步骤，请查看本轮生成的结构化工件。"


def _semantic_binding(record: EvidenceRecord, path: str) -> JsonObject:
    """把数值路径绑定到工具入参、不可变数据集及具体分组，不靠数值反推语义。"""
    context = record.summary.get("numeric_context") or {}
    context = {**context, **context.get("tool_args", {})}
    context = {**context, **context.get("encoding", {})}
    item: JsonObject = next(
        (item for item in record.summary.get("value_index", []) if item.get("path") == path),
        {},
    )
    if record.source.get("tool") == "generate_report" and isinstance(
        item.get("report_binding"), dict
    ):
        return {**item["report_binding"], "evidence_path": path}
    role = item.get("statistic") or statistic_role(path)
    source_tool = record.source.get("tool")
    statistical = source_tool in TOOL_LABELS or record.kind == "stats"
    obj = item.get("object") if statistical else None
    analysis = analysis_identity(context, source_tool, record.summary) if statistical else None
    counts = {
        "row_count",
        "count",
        "valid_value_count",
        "column_count",
        "group_total",
        "sample_size",
        "total_rows",
        "n_anomalies",
        "tests_count",
        "group_count",
        "excluded_rows",
        "minimum_required",
        "total_observations",
        "training_observations",
        "validation_observations",
        "protected_group_count",
        "protected_row_count",
    }
    metric = (
        item.get("metric") or context.get("value_col") or context.get("y_col") or context.get("y")
    )
    return {
        "dataset_ref": context.get("dataset_ref"),
        # DatasetRef 本身是不可变版本锚点；额外版本 hash 在可用时共同保存。
        "dataset_version": context.get("data_version_hash", context.get("dataset_ref")),
        "metric": role if role in counts else metric,
        "statistic": role,
        "statistic_scope": None if statistical else statistic_scope(path),
        "analysis": analysis,
        "object": obj,
        "aggregation": context.get("agg", context.get("method")),
        "filters": context.get("filters", []),
        "grain": context.get(
            "group_col", context.get("dimension_col", context.get("x_col", context.get("x")))
        ),
        "group_key": item.get("group_key"),
        "unit": (
            "count"
            if role in counts
            else "ratio"
            if statistical
            and role
            in {
                "corr",
                "p_value",
                "adjusted_p_value",
                "r_squared",
                "adj_r_squared",
                "alpha",
                "share",
                "seasonality_strength",
            }
            else (record.summary.get("analysis_context") or {}).get("slope_unit") or "raw"
            if statistical and role == "slope"
            else context.get("unit", "raw")
        ),
        "evidence_path": path,
    }


def _semantic_candidate_matches(
    value: tuple[str, str, Decimal, str],
    statement: str,
    match: re.Match[str],
    evidence: list[EvidenceRecord],
    dimensions: dict[str, set[str]],
    table_cells: tuple[tuple[str, str], ...] | None = None,
    value_span: tuple[int, int] | None = None,
) -> bool:
    """对完整数字子句绑定指标和分组；不按数字位置猜测或以同值证明关系。"""
    for shape in re.finditer(
        r"(?P<row_count>\d+)\s*行\s*[×x*]\s*(?P<column_count>\d+)\s*列", statement
    ):
        for dimension in ("row_count", "column_count"):
            if match.span() == shape.span(dimension):
                shape_record = next(record for record in evidence if record.evidence_id == value[0])
                binding = _semantic_binding(shape_record, value[1])
                return value[1].endswith("." + dimension) and identity_matches(
                    binding, statement, [binding], table_cells
                )
    dimension_spans = dimension_mentions(statement, dimensions)
    numbers = [
        number
        for number in _NUMBER_PATTERN.finditer(statement)
        if value_span is None or value_span[0] <= number.start() < number.end() <= value_span[1]
        if not any(
            start <= number.start() and number.end() <= end for start, end, _, _ in dimension_spans
        )
    ]
    local = statement
    if len(numbers) > 1:
        separators = [separator.start() for separator in re.finditer(r"[，,、：:]", statement)]
        for aside in re.finditer(r"[（(][^()（）]*[）)]", statement):
            if _NUMBER_PATTERN.search(aside.group(0)):
                separators.extend((aside.start(), aside.end() - 1))
        boundaries = [
            position
            for position in separators
            if not any(number.start() <= position < number.end() for number in numbers)
        ]
        left = max([pos + 1 for pos in boundaries if pos < match.start()], default=0)
        right = min([pos for pos in boundaries if pos >= match.end()], default=len(statement))
        local = statement[left:right]
        # Multiple measurements without a structural delimiter are ambiguous.
        if sum(left <= number.start() and number.end() <= right for number in numbers) > 1:
            return False
    bindings = [
        (record, _semantic_binding(record, str(item.get("path"))))
        for record in evidence
        for item in record.summary.get("value_index", [])
        if isinstance(item, dict) and item.get("source_kind") == "number"
    ]
    selected = next(
        (
            binding
            for record, binding in bindings
            if record.evidence_id == value[0] and binding["evidence_path"] == value[1]
        ),
        {},
    )
    if not identity_matches(selected, local, [binding for _, binding in bindings], table_cells):
        return False
    aggregations = {
        name
        for name, labels in {
            "sum": ("合计", "总计", "总额"),
            "mean": ("均值", "平均值", "平均"),
        }.items()
        if any(label in local for label in labels)
    }
    if selected.get("statistic") in {"value", "total_value"} and (
        len(aggregations) > 1
        or (
            aggregations
            and selected.get("aggregation")
            and selected.get("aggregation") not in aggregations
        )
    ):
        return False
    fields = {str(binding["metric"]) for _, binding in bindings if binding.get("metric")}
    metric_text = local
    if table_cells is not None:
        # A labelled grouping cell is not the measurement column. Keep the
        # original text/cells for identity, group, unit and statistic checks.
        for dimension, group in table_cells:
            if dimension in dimensions and group in dimensions[dimension]:
                metric_text = metric_text.replace(f"{dimension}={group}", "")
    mentioned = {field for field in fields if field in metric_text}
    # Prefer complete field names over their substrings, never a nearest-prefix guess.
    mentioned = {
        field
        for field in mentioned
        if not any(field != other and field in other for other in mentioned)
    }
    mentioned_groups = {
        (dimension, group) for _, _, dimension, group in dimension_mentions(local, dimensions)
    }
    structured_object = (selected.get("object") or {}).get("kind") in {
        "group_pair",
        "observation",
        "prediction",
    }
    if len(mentioned_groups) > 1 and not structured_object:
        return False
    if (
        mentioned_groups
        and not structured_object
        and (selected.get("grain"), selected.get("group_key")) not in mentioned_groups
    ):
        return False
    if selected.get("group_key") is not None and not mentioned_groups:
        return False
    role = str(selected.get("statistic"))
    roles = mentioned_roles(local, {str(binding["statistic"]) for _, binding in bindings})
    if roles:
        if len(roles) != 1:
            return False
        requested = next(iter(roles))
        if requested != role and not (
            role in {"value", "total_value"} and requested == selected.get("aggregation")
        ):
            return False
        if selected.get("unit") == "count" or not selected.get("metric"):
            return True
        return selected.get("metric") in mentioned
    found_labels = [
        (alias, leaf)
        for leaf, aliases in _PATH_ALIASES.items()
        for alias in aliases
        if alias in local
    ]
    labels = {
        leaf
        for alias, leaf in found_labels
        if not any(alias != other and alias in other for other, _ in found_labels)
    }
    # Unit-bearing counts remain count facts even when no explicit noun precedes them.
    suffix = statement[match.end() :]
    if re.match(r"\s*行(?:\b|[^\x00-\x7f]|$)", suffix) and not labels:
        labels.add("row_count")
    if re.match(r"\s*列(?:\b|[^\x00-\x7f]|$)", suffix) and not labels:
        labels.add("column_count")
    if re.match(r"\s*个(?:分组|月份|月)", suffix) and not labels:
        labels.add("group_total")
    if labels:
        return len(labels) == 1 and value[1].endswith("." + next(iter(labels)))
    if mentioned:
        return (
            role in {"value", "total_value"}
            and len(mentioned) == 1
            and selected.get("metric") in mentioned
        )
    if selected.get("metric"):
        if role not in {"value", "total_value"}:
            return False
        # A category can carry a single explicitly introduced metric across a list.
        grouped_fields = {
            str(binding["metric"])
            for _, binding in bindings
            if binding.get("group_key") is not None and binding.get("metric")
        }
        shared = {field for field in grouped_fields if field in statement}
        if mentioned_groups and len(shared) == 1 and selected.get("metric") in shared:
            return True
        # A percentage is an explicit raw-ratio display, not a different business metric.
        if "比例" in local and match.group(0).endswith("%"):
            return selected.get("unit") != "count"
        # A governed definition supplies an explicit metric identity for its generic label.
        record = next((record for record in evidence if record.evidence_id == value[0]), None)
        if (
            record is not None
            and record.source.get("definition_execution")
            and re.match(r"^受控汇总值(?:为|是|[:：])?\s*[-+\d]", local)
        ):
            context = record.summary.get("numeric_context") or {}
            context = {**context, **context.get("tool_args", {})}
            return selected.get("metric") == context.get("value_col")
        return False
    # A statistic cannot become a business amount merely because the tool lacks
    # value_col (for example correlation takes columns instead). Legacy plain
    # measurements keep their existing behavior; statistical tools need a role.
    record = next((record for record in evidence if record.evidence_id == value[0]), None)
    # Legacy total-only tool Evidence has no arguments. Its metric is checked
    # against the invocation by the total-delivery contract. Report artifacts
    # and current Evidence always supply context and cannot use this branch.
    if (
        record is not None
        and record.kind == "tool_result"
        and record.source.get("tool") == "dimension_contribution"
        and not record.summary.get("numeric_context")
        and role == "total_value"
        and aggregations == {"sum"}
    ):
        return True
    return role not in ROLE_LABELS and (
        record is None
        or (record.source.get("tool") not in STATISTICAL_TOOLS and record.kind != "stats")
    )


def _evidence_values(
    evidence: list[EvidenceRecord],
) -> list[tuple[str, str, Decimal, str]]:
    indexed: list[tuple[str, str, Decimal, str]] = []
    for record in evidence:
        raw_index = record.summary.get("value_index")
        if not isinstance(raw_index, list):
            continue
        for raw in raw_index:
            if not isinstance(raw, dict):
                continue
            if raw.get("source_kind") != "number":
                continue
            path = raw.get("path")
            value = raw.get("value")
            if not isinstance(path, str) or not isinstance(value, str):
                continue
            normalized = _normalize_number(value)
            if normalized is not None:
                indexed.append((record.evidence_id, path, normalized, value))
    return indexed


def _match_evidence_value(
    claim_value: Decimal,
    values: list[tuple[str, str, Decimal, str]],
    *,
    statement: str,
    token: str,
    token_end: int,
    evidence_is_typed: bool = True,
) -> tuple[
    tuple[str, str, str] | None,
    list[tuple[str, str, str]],
    str | None,
    Decimal,
]:
    display_factor = _display_factor(token, statement=statement, token_end=token_end)
    exact_candidates = [
        (evidence_id, path, raw_value)
        for evidence_id, path, evidence_value, raw_value in values
        if display_factor == Decimal(1) and evidence_value == claim_value
    ]
    if exact_candidates:
        return (
            _select_evidence_candidate(exact_candidates, statement),
            exact_candidates,
            "exact",
            Decimal(1),
        )

    quantum = _display_quantum(token)
    if display_factor is None or quantum is None:
        return None, [], None, Decimal(1)
    candidates = [
        (evidence_id, path, raw_value)
        for evidence_id, path, evidence_value, raw_value in values
        if _matches_display_value(
            evidence_value,
            claim_value,
            display_factor=display_factor,
            quantum=quantum,
        )
    ]
    if not candidates:
        return None, [], None, display_factor
    match_kind = "unit_scaled" if display_factor != Decimal(1) else "rounded"
    return (
        _select_evidence_candidate(candidates, statement),
        candidates,
        match_kind,
        display_factor,
    )


def _select_evidence_candidate(
    candidates: list[tuple[str, str, str]], statement: str
) -> tuple[str, str, str]:
    if len(candidates) == 1:
        return candidates[0]

    scored = [(_path_relevance(item[1], statement), item) for item in candidates]
    highest = max(score for score, _item in scored)
    best = [item for score, item in scored if score == highest]
    # Prefer a unique semantic path match. Otherwise retain every candidate in
    # the Claim instead of silently pretending the first path is unambiguous.
    return best[0] if highest > 0 and len(best) == 1 else candidates[0]


def _display_factor(token: str, *, statement: str, token_end: int) -> Decimal | None:
    """Return raw-evidence units represented by one displayed claim unit.

    For example, ``10.28万`` represents a raw value near ``102800`` and
    ``25.1%`` represents a raw ratio near ``0.251``. Combining percent with a
    large-number unit is rejected instead of guessed.
    """
    suffix = statement[token_end : token_end + 4]
    percent = token.endswith("%") or re.match(r"\s*%", suffix) is not None
    unit_match = re.match(r"\s*([千万亿])", suffix)
    if percent and unit_match is not None:
        return None
    if percent:
        return Decimal("0.01")
    if unit_match is not None:
        return _DISPLAY_UNIT_FACTORS[unit_match.group(1)]
    return Decimal(1)


def _display_quantum(token: str) -> Decimal | None:
    normalized = _normalize_number(token)
    if normalized is None:
        return None
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return Decimal(1).scaleb(exponent)


def _matches_display_value(
    evidence_value: Decimal,
    claim_value: Decimal,
    *,
    display_factor: Decimal,
    quantum: Decimal,
) -> bool:
    """Accept only values that deterministically render as the claim token."""
    try:
        displayed = evidence_value / display_factor
        return displayed.quantize(quantum, rounding=ROUND_HALF_UP) == claim_value
    except (InvalidOperation, ZeroDivisionError):
        return False


def _path_relevance(path: str, statement: str) -> int:
    lowered = statement.casefold().replace("_", "")
    parts = [part for part in re.split(r"[.\[\]#]", path) if part]
    leaf = parts[-1].casefold() if parts else ""
    score = 0
    if leaf and leaf.replace("_", "") in lowered:
        score += 3
    for field, aliases in _PATH_ALIASES.items():
        if field in path and any(alias.casefold() in lowered for alias in aliases):
            score += 2
    return score


def _looks_like_time_scope(statement: str, start: int, end: int) -> bool:
    """Exclude explicit time-scope restatements, not arbitrary goal-equal values."""
    before = statement[max(0, start - 8) : start]
    after = statement[end : end + 8]
    if re.match(r"\s*(?:年|月|日|季度|季|周|时|分|秒)", after):
        return True
    return bool(
        re.search(r"(?:按|在|从|至|截至|期间|范围)\s*$", before)
        and re.match(r"\s*(?:年度|月份|日期|季度|周期)", after)
    )


def _split_statements(text: str) -> list[str]:
    # findall with the deliberately simple boundary expression avoids an LLM
    # sentence splitter while keeping the original statement for audit display.
    statements: list[str] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", text):
        end = match.end()
        statements.append(text[start:end])
        start = end
    if start < len(text):
        statements.append(text[start:])
    return statements


def _numeric_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _NUMBER_PATTERN.finditer(text)]


def _normalize_number(token: str) -> Decimal | None:
    clean = token.rstrip("%").replace(",", "")
    try:
        value = Decimal(clean)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _decimal_from_value(value: int | float | Decimal) -> Decimal | None:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        normalized = Decimal(str(value))
    except InvalidOperation:
        return None
    return normalized if normalized.is_finite() else None
