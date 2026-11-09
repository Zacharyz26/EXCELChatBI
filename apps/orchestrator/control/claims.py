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
import unicodedata
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from packages.common.exact_numbers import decode_exact_number
from packages.common.text_surface import SourceProjection, parse_text_surface
from packages.orchestration.artifact_counts import verified_chart_deliveries
from packages.orchestration.contracts import REPORT_QUANTITY_VALIDATOR
from packages.orchestration.evidence_facts import OPERATIONS, valid_count_projection
from packages.orchestration.quality_facts import quality_projection
from packages.session.models import Artifact, JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord, ToolInvocation

from apps.orchestrator.control.claim_structure import (
    AggregateMemberContext,
    ProfileProseContext,
    ProfileTableContext,
    dimension_mentions,
    split_claim_statements,
    statement_projections,
)
from apps.orchestrator.control.count_claims import (
    CHINESE_COUNT,
    chart_delivery_role,
    column_quantity_matches,
    count_noun,
    operation_quantity,
    operation_scope_matches,
    parse_chinese_count,
)
from apps.orchestrator.control.dataset_identity import (
    IDENTITY_FIELDS,
    identity_declarations,
    identity_literal,
)
from apps.orchestrator.control.numeric_display import NUMBER_PATTERN as _NUMBER_PATTERN
from apps.orchestrator.control.numeric_display import decimal_from_token as _normalize_number
from apps.orchestrator.control.numeric_display import display_quantum as _display_quantum
from apps.orchestrator.control.numeric_display import (
    matches_display_value as _matches_display_value,
)
from apps.orchestrator.control.numeric_display import (
    measurement_value_context,
    numeric_quote_view,
    parse_numeric_display,
    unrecognized_numeric_spans,
)
from apps.orchestrator.control.profile_claims import (
    annotate_profile_values,
    explicit_field_unit_matches,
    has_column_profile,
    parse_profile_stat_header,
    profile_assignment_role,
    profile_candidate_matches,
    profile_fact,
    profile_field_identity_spans,
    profile_statement_context,
    profile_statistic_labels,
    unit_qualified_field_mentions,
)
from apps.orchestrator.control.profile_population import (
    PopulationRequest,
    mask_population,
    population_filters,
    population_matches,
    requested_population,
    stated_population,
)
from apps.orchestrator.control.semantic_identity import (
    TOOL_LABELS,
    analysis_identity,
    annotate_objects,
    identity_matches,
    table_column_role,
)
from apps.orchestrator.control.statistic_roles import (
    ROLE_LABELS,
    STATISTICAL_TOOLS,
    mentioned_roles,
    statistic_role,
    statistic_scope,
)
from apps.orchestrator.control.statistic_statements import (
    ASSIGNMENT_OPERATOR,
    StatisticValueEvaluation,
    count_expressions,
    statistic_value_owner,
    statistic_value_slots,
)

_LEADING_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s+|\d+[.)、]\s*)")
_MAX_EVIDENCE_VALUES = 256
_MAX_EVIDENCE_SOURCES = 64
_MAX_VALUE_DEPTH = 8
_NO_KNOWLEDGE_RESULT_PATTERN = re.compile(
    r"(?:未找到|没有找到|没查到|未检索到|没有检索到|没能检索到|无相关|没有相关|检索无结果|"
    r"无法(?:依据|从).{0,12}(?:回答|确认)|"
    r"知识库.{0,8}(?:没有|无|未收录))"
)
_LIMITATION_PATTERN = re.compile(
    r"(?:局限|限制|注意|仅供参考|不能代表|不代表因果|样本不足|缺少|不确定)"
)
_PATH_ALIASES: dict[str, tuple[str, ...]] = {
    "rate": ("转化率",),
    "ratio": ("比例", "比率"),
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
        and report_validation.get("validator") == REPORT_QUANTITY_VALIDATOR
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
    annotate_profile_values(values, result)
    quality = quality_projection(result, dict(arguments or {}))
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
        **({"quality_projection": quality} if quality is not None else {}),
    }


def extract_claims(
    *,
    final_text: str,
    goal: str,
    evidence: list[EvidenceRecord],
    run_id: str | None = None,
    invocations: list[ToolInvocation] | None = None,
    artifacts: list[Artifact] | None = None,
) -> list[ClaimDraft]:
    """Extract every deterministic Stage-1 Claim type from a candidate answer."""
    claims = extract_numeric_claims(
        final_text=final_text,
        goal=goal,
        evidence=evidence,
        run_id=run_id,
        invocations=invocations,
        artifacts=artifacts,
    )
    claims.extend(extract_knowledge_claims(final_text=final_text, evidence=evidence))
    claims.extend(extract_limitation_claims(final_text=final_text))
    return claims


def _verified_identity_spans(
    statement: str, evidence: list[EvidenceRecord], source_labels: set[str]
) -> tuple[list[tuple[int, int]], list[JsonObject]]:
    """Protect only explicit identity headers backed by one current source.

    A header starts a clause. Its literal value alone is exempt, never the
    surrounding prose or a repeated hash in a measurement's value position.
    Invalid or unknown declarations remain explicit unsupported claims, even
    if their spelling contains no decimal digit.
    """
    declarations: list[tuple[str, str, int, int]] = []
    field_predicate = re.compile(
        r"(?:的)?(?:" + "|".join(re.escape(label) for label in _measurement_labels()) + r"|是|为)"
    )
    for match in identity_declarations(statement, headers_only=True):
        # An actual source field followed by its predicate is a field statement,
        # even when its full name starts with an identity-header word.
        if any(
            statement.startswith(field, match.start())
            and field_predicate.match(statement[match.start() + len(field) :])
            for field in source_labels - _measurement_labels()
        ):
            continue
        if measurement_value_context(statement, match.start(), match.end(), _measurement_labels()):
            continue
        key = IDENTITY_FIELDS[match["label"]]
        # A conventional release label is not a dataset version hash.
        if not (match["label"] == "版本" and re.fullmatch(r"v\d+(?:\.\d+)+", match["value"])):
            declarations.append((key, match["value"], *match.span("value")))
    if not declarations:
        return [], []
    contexts = [
        context
        for record in evidence
        if record.kind in {"tool_result", "profile", "stats", "chart", "table"}
        if isinstance(context := record.summary.get("numeric_context") or {}, dict)
    ]
    for record in evidence:
        projection = record.summary.get("fact_projection")
        if isinstance(projection, dict) and valid_count_projection(
            record.summary,
            tool=str(record.source.get("tool")),
            run_id=record.run_id,
            invocation_id=record.invocation_id,
            result_hash=record.result_hash,
        ):
            contexts.extend(
                {
                    "dataset_ref": projection.get(side),
                    "data_version_hash": projection.get("branch_hash"),
                }
                for side in ("input_ref", "output_ref")
            )
    valid = all(identity_literal(value) for _, value, _, _ in declarations)
    valid = valid and any(
        all(
            value
            == (
                context.get("dataset_ref")
                if key == "dataset_ref"
                else context.get("data_version_hash", context.get("dataset_ref"))
            )
            for key, value, _, _ in declarations
        )
        for context in contexts
    )
    if valid:
        return [(start, end) for _, _, start, end in declarations], []
    return [], [
        {
            "token": value,
            "supported": False,
            "reason": "unverified_dataset_identity",
            "statement_span": [start, end],
        }
        for _, value, start, end in declarations
    ]


def verified_table_structure_spans(
    text: str, evidence: list[EvidenceRecord]
) -> tuple[tuple[int, int], ...]:
    """证明原表格身份格/主体槽序号，不豁免表头内的其他业务声明。

    身份列必须作为同一元组匹配一个真实来源；主体序号只说明已有
    关系表结构中的列位置。主体成员是否正确仍须通过关系绑定器。
    """
    surface = parse_text_surface(text)
    tables = sorted({node.table for node in surface.nodes if node.table is not None})
    if not tables:
        return ()
    spans: list[tuple[int, int]] = []
    bindings = [
        _semantic_binding(record, str(item.get("path")))
        for record in evidence
        if record.source.get("tool") in STATISTICAL_TOOLS
        if isinstance(record.summary.get("numeric_context") or {}, dict)
        for item in record.summary.get("value_index", [])
        if isinstance(item, dict)
    ]
    relation_bindings = [
        binding
        for binding in bindings
        if isinstance(obj := binding.get("object"), dict)
        and isinstance(obj.get("kind"), str)
        and obj.get("kind") in {"column_pair", "group_pair"}
    ]
    for table in tables:
        headers = sorted(
            (node for node in surface.nodes if node.table == table and node.kind == "table_header"),
            key=lambda node: node.column or 0,
        )
        labels = [text[slice(*node.content_span)].strip().strip("*`_").strip() for node in headers]
        rows = sorted(
            {
                node.row
                for node in surface.nodes
                if node.table == table and node.kind == "table_cell" and node.row is not None
            }
        )
        complete_rows = []
        for row in rows:
            cells = sorted(
                (
                    node
                    for node in surface.nodes
                    if node.table == table and node.row == row and node.kind == "table_cell"
                ),
                key=lambda node: node.column or 0,
            )
            if len(cells) != len(headers) or not all(node.closed for node in cells):
                continue
            complete_rows.append(cells)
            identities = [
                (labels[index], text[slice(*cell.content_span)].strip().strip("*`_").strip(), cell)
                for index, cell in enumerate(cells)
                if labels[index] in IDENTITY_FIELDS
            ]
            if identities:
                # Clause positions are synthetic here only to reuse the exact
                # identity tuple checker. Protection uses original cell spans.
                declarations = "；".join(f"{label}={value}" for label, value, _ in identities)
                protected, errors = _verified_identity_spans(declarations, evidence, set())
                if not errors and len(protected) == len(identities):
                    spans.extend(cell.content_span for _, _, cell in identities)
        if not complete_rows:
            continue
        for binding in relation_bindings:
            kind = str(binding["object"]["kind"])
            subjects = [
                index
                for index, label in enumerate(labels)
                if table_column_role(label, kind) == "subject"
            ]
            if len(subjects) != 2 or not any(
                mentioned_roles(label, {str(binding.get("statistic"))}) for label in labels
            ):
                continue
            # These are reserved structural role names, not arbitrary nouns.
            # The relation's object type chooses the grammar; a default
            # "subject" classification alone grants no ordinal exemption.
            role_name = r"(?:变量|列|column|variable)" if kind == "column_pair" else r"(?:组|group)"
            for ordinal, index in enumerate(subjects, 1):
                raw = text[slice(*headers[index].content_span)]
                # Full reserved role + ordinal, or ordinal marker + role.
                # Extra nouns/clauses cannot be discarded as label metadata.
                match = re.fullmatch(
                    r"\s*(?:" + role_name + r"\s*(?P<suffix>[0-9一二三四五六七八九十]+)"
                    r"|(?P<prefix>[0-9一二三四五六七八九十]+)[.)、]\s*" + role_name + r")\s*",
                    raw,
                    re.I,
                )
                if match is None:
                    continue
                group = "suffix" if match["suffix"] is not None else "prefix"
                token = match[group]
                value = (
                    int(token)
                    if token.isascii() and token.isdigit()
                    else parse_chinese_count(token)
                )
                left, right = match.span(group)
                # Slot classification is not authority to erase a value
                # assignment. Use the shared predicate grammar with its actual
                # left-hand owner, including an unknown field. This establishes
                # an obligation only, never an Evidence-supported role.
                assignment_owners = {
                    raw[: operator.start()].strip()
                    for operator in re.finditer(ASSIGNMENT_OPERATOR, raw)
                    if raw[: operator.start()].strip()
                }
                assigned = measurement_value_context(
                    raw, left, right, assignment_owners, assignment_only=True
                )
                if (
                    value == ordinal
                    and not measurement_value_context(raw, left, right, _measurement_labels())
                    and not assigned
                ):
                    offset = headers[index].content_span[0]
                    # The prefix marker's punctuation belongs to the ordinal,
                    # just like a source list marker; it is not a decimal dot.
                    spans.append((offset + left, offset + right + (group == "prefix")))
    return tuple(sorted(set(spans)))


def extract_numeric_claims(
    *,
    final_text: str,
    goal: str,
    evidence: list[EvidenceRecord],
    evidence_is_typed: bool = True,
    strip_list_markers: bool = True,
    run_id: str | None = None,
    invocations: list[ToolInvocation] | None = None,
    artifacts: list[Artifact] | None = None,
    chart_source_evidence: list[EvidenceRecord] | None = None,
) -> list[ClaimDraft]:
    """先核对文档所有者/完成义务，再绑定残余业务事实，保留两种审计。"""
    from apps.orchestrator.control.quantity_presentation import is_draft_candidate, parse_draft
    from apps.orchestrator.control.quantity_surface import analyze_surface

    if is_draft_candidate(final_text):
        try:
            final_text = parse_draft(final_text, evidence, goal=goal)
        except (ValueError, TypeError, RecursionError):
            return [
                ClaimDraft(
                    statement=final_text,
                    claim_kind="numeric",
                    value_refs=(
                        {
                            "token": "[draft]",
                            "supported": False,
                            "reason": "invalid_quantity_protocol",
                        },
                    ),
                    evidence_ids=(),
                )
            ]

    options: dict[str, Any] = dict(
        goal=goal,
        evidence=evidence,
        evidence_is_typed=evidence_is_typed,
        # SurfacePlan has consumed the actual source markers once. A remaining
        # numeric fragment (e.g. the body of "1. 1.") is not a second marker.
        strip_list_markers=False,
        run_id=run_id,
        invocations=invocations,
        artifacts=artifacts,
        chart_source_evidence=chart_source_evidence,
    )

    def fact_member(text: str) -> list[ClaimDraft]:
        return _extract_numeric_claims(final_text=text, **options)

    surface = analyze_surface(final_text, evidence, fact_resolver=fact_member, goal=goal)
    for ref in surface.refs:
        span = ref.get("statement_span")
        if (
            isinstance(span, list | tuple)
            and len(span) == 2
            and all(type(value) is int for value in span)
            and 0 <= span[0] < span[1] <= len(final_text)
            and ref.get("token") == final_text[slice(*span)]
        ):
            ref["source_span"] = list(span)
            ref["source_token"] = final_text[slice(*span)]
        else:
            ref["supported"] = False
            ref["reason"] = "invalid_prebound_source_projection"
    # Prebound quantities keep their source spelling for neighboring field/scope
    # grammar. Only their exact source spans are excluded from rebinding; no
    # surrounding heading or statistical sentence is erased with them.
    numeric_view = list(surface.numeric_text)
    prebound_spans = []
    for ref in surface.refs:
        span = ref.get("statement_span")
        if isinstance(span, list | tuple) and len(span) == 2:
            left, right = span
            numeric_view[left:right] = final_text[left:right]
            prebound_spans.append((left, right))
    claims = _extract_numeric_claims(
        final_text="".join(numeric_view),
        structure_source=final_text,
        prebound_spans=tuple(prebound_spans),
        **options,
    )
    refs = [
        *surface.refs,
        *(
            {
                "token": "[" + issue + "]",
                "supported": False,
                "reason": issue,
                "ref_kind": "quantity_audit_issue",
                "quantity_purpose": "unknown",
            }
            for issue in surface.issues
        ),
    ]
    if refs:
        claims.append(
            ClaimDraft(
                statement=final_text,
                claim_kind="numeric",
                value_refs=tuple(refs),
                evidence_ids=tuple(
                    dict.fromkeys(
                        ref["evidence_id"]
                        for ref in refs
                        if ref.get("supported") and ref.get("evidence_id")
                    )
                ),
            )
        )
    from apps.orchestrator.control.quantity_presentation import quantity_audit

    quantities, uncovered = quantity_audit(final_text, evidence, claims, goal=goal)
    audits = {tuple(q["source_span"]): q for q in quantities}
    for claim in claims:
        for ref in claim.value_refs:
            span = ref.get("source_span")
            if span is None and claim.statement == final_text:
                span = ref.get("statement_span")
            if isinstance(span, list | tuple) and (audit := audits.get(tuple(span))):
                ref["quantity_audit"] = audit
    if uncovered:
        claims.append(
            ClaimDraft(
                statement=final_text,
                claim_kind="numeric",
                value_refs=tuple(
                    {
                        "token": "[coverage]",
                        "supported": False,
                        "reason": issue,
                        "ref_kind": "quantity_audit_issue",
                    }
                    for issue in uncovered
                ),
                evidence_ids=(),
            )
        )
    return claims


def _extract_numeric_claims(
    *,
    final_text: str,
    goal: str,
    evidence: list[EvidenceRecord],
    evidence_is_typed: bool = True,
    strip_list_markers: bool = True,
    run_id: str | None = None,
    invocations: list[ToolInvocation] | None = None,
    artifacts: list[Artifact] | None = None,
    chart_source_evidence: list[EvidenceRecord] | None = None,
    prebound_spans: tuple[tuple[int, int], ...] = (),
    structure_source: str | None = None,
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
    chart_deliveries = verified_chart_deliveries(
        run_id=run_id,
        # Legacy measurement binding may enrich args; delivery identity must
        # cross-check the original persisted context, never that merged view.
        evidence=evidence if chart_source_evidence is None else chart_source_evidence,
        invocations=invocations or [],
        artifacts=artifacts or [],
    )
    evidence = [
        replace(record, summary={**record.summary, "value_index": [], "numeric_context": {}})
        if "fact_projection" in record.summary
        and not valid_count_projection(
            record.summary,
            tool=str(record.source.get("tool")),
            run_id=record.run_id,
            invocation_id=record.invocation_id,
            result_hash=record.result_hash,
        )
        else record
        for record in evidence
    ]
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
    bindings = [
        _semantic_binding(record, str(item.get("path")))
        for record in evidence
        for item in record.summary.get("value_index", [])
    ]
    measurement_labels = _measurement_labels() | profile_statistic_labels(final_text)
    source_labels = {
        str(binding[key]) for binding in bindings for key in ("metric", "grain") if binding.get(key)
    } | measurement_labels
    claims: list[ClaimDraft] = []
    profile_fields = {
        str(b["object"]["column"])
        for b in bindings
        if b.get("profile_fact") and b.get("object", {}).get("kind") == "profile_column"
    }
    statements = statement_projections(
        final_text,
        measurement_labels,
        source_labels=source_labels,
        profile_fields=profile_fields,
        dimensions=set(dimensions),
        structure_source=structure_source,
    )
    table_structure_spans = verified_table_structure_spans(final_text, evidence)
    profile_context = ""
    required_population = requested_population(goal, profile_fields)
    population_context = required_population
    for projected in statements:
        raw_statement = projected.text
        table_cells = projected.cells
        value_span = projected.value_span
        heading_context = projected.heading_context
        source_map = projected.source_map
        # Only a semicolon in the same prose item can carry a profile subject.
        # A table/list boundary precedes the statement, while a trailing raw
        # newline closes its context only after this statement is evaluated.
        # Keep the actual Claim statement unchanged; context is verification-only.
        starts_profile_item = (
            table_cells is not None or _LEADING_LIST_MARKER.match(raw_statement) is not None
        )
        if starts_profile_item:
            profile_context = ""
        inherited_profile_context = profile_context or heading_context
        has_profile_line_break = any(char in raw_statement.lstrip(" \t\r\n") for char in "\r\n")
        closes_profile_item = starts_profile_item or has_profile_line_break
        statement = (
            raw_statement
            if table_cells is not None or not strip_list_markers
            else _LEADING_LIST_MARKER.sub("", raw_statement)
        ).strip()
        if (
            table_cells is None
            and not has_profile_line_break
            and "\n" not in statement
            and raw_statement.rstrip(" \t\r").endswith((";", "；"))
        ):
            profile_context = inherited_profile_context + statement
        elif not closes_profile_item:
            profile_context = profile_statement_context(statement, profile_fields)
        else:
            profile_context = ""
        if not statement:
            continue
        marker = (
            _LEADING_LIST_MARKER.match(raw_statement)
            if table_cells is None and strip_list_markers
            else None
        )
        trim_start = marker.end() if marker else 0
        trim_start += len(raw_statement[trim_start:]) - len(raw_statement[trim_start:].lstrip())
        statement_source_map = source_map[trim_start : trim_start + len(statement)]
        # Scope can be inherited from the enclosing original header, but its
        # offsets must never be applied to a separately projected child text.
        local_population = stated_population(statement, profile_fields)
        enclosing_population = (
            stated_population(projected.constraint_context, profile_fields)
            if projected.source_context
            else None
        )
        if enclosing_population is not None:
            population_context = enclosing_population
        if local_population is not None:
            population_context = local_population
        semantic_statement, quoted_labels = numeric_quote_view(statement, measurement_labels)
        profile_header = parse_profile_stat_header(
            semantic_statement, profile_fields, context=semantic_statement
        )
        population_in_header = bool(
            local_population
            and profile_header
            and profile_header.span[0] <= local_population.span[0]
            and local_population.span[1] <= profile_header.span[1]
        )
        population_statement = (
            semantic_statement
            if population_in_header
            else mask_population(semantic_statement, local_population)
        )
        refs: list[JsonObject] = []
        reference_positions: dict[int, int] = {}
        slots = (
            []
            if projected.literal_label
            else statistic_value_slots(statement, measurement_labels, source_labels)
        )
        evaluations: list[StatisticValueEvaluation] = []
        linked_ids: list[str] = []
        labels = dimension_mentions(statement, dimensions)
        identity_spans, identity_errors = _verified_identity_spans(
            statement, evidence, source_labels
        )
        if projected.literal_label:
            identity_spans.append((0, len(statement)))
        if local_population is not None and local_population.valid:
            identity_spans.append(local_population.span)
        identity_spans.extend(quoted_labels)
        for source_left, source_right in (*prebound_spans, *table_structure_spans):
            prebound_positions = [
                i
                for i, position in enumerate(statement_source_map)
                if position is not None and source_left <= position < source_right
            ]
            if prebound_positions and len(prebound_positions) == source_right - source_left:
                identity_spans.append((prebound_positions[0], prebound_positions[-1] + 1))
        # Literal field identity comes from the current source, including units
        # embedded in its actual name. Those glyphs are not measured values.
        identity_spans.extend(profile_field_identity_spans(statement, profile_fields))
        counts = count_expressions(
            statement, measurement_labels=measurement_labels, owner_fields=source_labels
        )
        identity_spans.extend(item.value_span for item in counts if item.requested_output)
        # A calendar month attached directly to a named measurement is a time
        # label. It is distinct from an explicit cardinal such as 七个月份.
        for item in counts:
            left, right = item.value_span
            month = parse_chinese_count(statement[left:right])
            if (
                month is not None
                and 1 <= month <= 12
                and statement.startswith("月", right)
                and re.search(r'(?:^|[，,。；;：:\n])\s*[（("“]*$', statement[:left])
            ):
                identity_spans.append(item.value_span)
        refs.extend(identity_errors)

        def supported_count_role(span: tuple[int, int], text: str = statement) -> bool:
            quantity = operation_quantity(text, *span)
            return (
                (quantity is not None and quantity.role in {"before", "after", "removed"})
                or count_noun(text, *span) in {"分组", *dimensions}
                or chart_delivery_role(text, *span) is not None
            )

        unresolved_counts = [
            item.value_span
            for item in counts
            if not any(a <= item.value_span[0] < item.value_span[1] <= b for a, b in identity_spans)
            and (
                not item.noun
                or (
                    _normalize_number(statement[slice(*item.value_span)]) is None
                    and parse_chinese_count(statement[slice(*item.value_span)]) is None
                )
                or (
                    CHINESE_COUNT.fullmatch(statement[slice(*item.value_span)])
                    and not supported_count_role(item.value_span)
                )
            )
        ]
        for left, right in unresolved_counts:
            if owner := statistic_value_owner(statement, (left, right), slots):
                evaluations.append(StatisticValueEvaluation(owner.statement_span, (left, right)))
            refs.append(
                {
                    "token": statement[left:right],
                    "supported": False,
                    "reason": "unrecognized_numeric_expression",
                    "statement_span": [left, right],
                }
            )
        numeric_matches = [
            match
            for match in _NUMBER_PATTERN.finditer(statement)
            if not any(a <= match.start() and match.end() <= b for a, b in identity_spans)
            and not any(a <= match.start() and match.end() <= b for a, b in unresolved_counts)
        ]
        # Promote only typed integer counts. Other Chinese quantities keep the
        # existing unknown classification and original token/span below.
        numeric_matches.extend(
            match
            for match in CHINESE_COUNT.finditer(statement)
            if parse_chinese_count(match.group()) is not None
            and not any(a <= match.start() and match.end() <= b for a, b in identity_spans)
            and supported_count_role(match.span())
            and not any(a <= match.start() and match.end() <= b for a, b in unresolved_counts)
        )
        numeric_matches.sort(key=lambda match: match.start())
        covered = [
            *identity_spans,
            *unresolved_counts,
            *[match.span() for match in numeric_matches],
        ]
        covered.extend((start, end) for start, end, _, _ in labels)
        # Only entire known textual labels protect embedded numeric glyphs;
        # a numeric-only label cannot exempt a same-value measurement elsewhere.
        covered.extend(
            match.span()
            for label in source_labels
            if any(
                char.isalpha() and not unicodedata.category(char).startswith("N") for char in label
            )
            for match in re.finditer(re.escape(label), statement)
        )
        for start, end in unrecognized_numeric_spans(
            statement, covered, measurement_labels=measurement_labels
        ):
            if value_span is None or value_span[0] <= start < end <= value_span[1]:
                if owner := statistic_value_owner(statement, (start, end), slots):
                    evaluations.append(StatisticValueEvaluation(owner.statement_span, (start, end)))
                refs.append(
                    {
                        "token": statement[start:end],
                        "supported": False,
                        "reason": "unrecognized_numeric_expression",
                        "statement_span": [start, end],
                    }
                )
        for match in numeric_matches:
            if value_span is not None and not (
                value_span[0] <= match.start() < match.end() <= value_span[1]
            ):
                continue
            if any(start <= match.start() and match.end() <= end for start, end, _, _ in labels):
                continue
            token = match.group(0)
            normalized = _normalize_number(token)
            chinese_count = parse_chinese_count(token) if normalized is None else None
            if chinese_count is not None:
                normalized = Decimal(chinese_count)
            if normalized is None or (
                normalized in goal_numbers
                and _looks_like_time_scope(statement, match.start(), match.end())
            ):
                continue
            delivery_role = chart_delivery_role(statement, *match.span())
            if delivery_role is not None:
                ref: JsonObject = {
                    "kind": "delivery_count",
                    "token": token,
                    "normalized": str(normalized),
                    "supported": False,
                    "count_basis": delivery_role,
                    "statement_span": list(match.span()),
                }
                if (
                    delivery_role in {"chart_dimensions", "chart_artifacts"}
                    and chart_deliveries
                    and not any(
                        item.qualified and item.value_span == match.span() for item in counts
                    )
                ):
                    expected = len(
                        {item["dimension"] for item in chart_deliveries}
                        if delivery_role == "chart_dimensions"
                        else {item["artifact_id"] for item in chart_deliveries}
                    )
                    binding = {
                        "dataset_ref": chart_deliveries[0]["dataset_ref"],
                        "dataset_version": chart_deliveries[0]["dataset_version"],
                        "statistic": delivery_role,
                        "unit": "count",
                        "filters": [],
                    }
                    if normalized == expected and identity_matches(
                        binding, statement, [binding], table_cells
                    ):
                        ref.update(
                            supported=True,
                            evidence_id=chart_deliveries[0]["evidence_id"],
                            sources=chart_deliveries,
                            semantic_binding=binding,
                            evidence_value=str(expected),
                        )
                        linked_ids.extend(
                            item["evidence_id"]
                            for item in chart_deliveries
                            if item["evidence_id"] not in linked_ids
                        )
                refs.append(ref)
                continue
            owner = statistic_value_owner(statement, match.span(), slots)
            if owner is not None:
                evaluations.append(StatisticValueEvaluation(owner.statement_span, match.span()))
            semantic_candidates = [
                value
                for value in evidence_values
                if _semantic_candidate_matches(
                    value,
                    semantic_statement,
                    match,
                    evidence,
                    dimensions,
                    table_cells,
                    value_span,
                    inherited_profile_context,
                    population_context,
                    population_statement,
                    required_population,
                    projected.table_context,
                    projected.source_context,
                    projected.constraint_context,
                    projected.aggregate_context,
                    projected.profile_prose_context,
                )
                and (
                    owner is None
                    or not _semantic_binding(evidence_by_id[value[0]], value[1]).get("profile_fact")
                    or profile_assignment_role(semantic_statement[: owner.value_span[0]])
                    == _semantic_binding(evidence_by_id[value[0]], value[1]).get("statistic")
                )
                and (
                    not parse_numeric_display(token, semantic_statement[match.end() :]).percent
                    or _semantic_binding(evidence_by_id[value[0]], value[1]).get("unit") == "ratio"
                )
            ]
            profile_candidates = [
                (value, _semantic_binding(evidence_by_id[value[0]], value[1]))
                for value in semantic_candidates
                if _semantic_binding(evidence_by_id[value[0]], value[1]).get("profile_fact")
            ]
            profile_identities = {
                json.dumps(
                    {k: v for k, v in binding.items() if k != "evidence_path"},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for _, binding in profile_candidates
            }
            if len(profile_identities) > 1 or len({v[2] for v, _ in profile_candidates}) > 1:
                semantic_candidates = []
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
                statement=semantic_statement,
                token=str(normalized) if chinese_count is not None else token,
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
                        "statement_span": [match.start(), match.end()],
                        **(
                            {"reason": "unrecognized_numeric_expression"}
                            if chinese_count is not None and not semantic_candidates
                            else {}
                        ),
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
                "statement_span": [match.start(), match.end()],
                **(
                    {
                        "statistic_slot_span": list(owner.statement_span),
                    }
                    if owner
                    else {}
                ),
                "semantic_binding": _semantic_binding(evidence_by_id[evidence_id], path),
            }
            if projected.table_context is not None:
                context = projected.table_context
                value_ref["source_structure"] = {
                    "kind": "table_cell",
                    "field_span": list(context.field_span),
                    "role_span": list(context.role_span),
                    "value_span": list(context.value_source_span),
                }
            if display_factor != Decimal(1):
                value_ref["display_factor"] = str(display_factor)
            if len(candidates) > 1:
                value_ref["candidate_paths"] = [
                    {"evidence_id": item[0], "path": item[1]} for item in candidates
                ]
                # Repeated producer paths for one exact immutable identity and
                # value are corroboration, not competing facts. Identity/value
                # conflicts were rejected before numerical matching above.
                value_ref["ambiguous_value"] = len(identities) > 1
            refs.append(value_ref)
            reference_positions[id(value_ref)] = match.start()
            for candidate_id, _candidate_path, _candidate_value in candidates:
                if candidate_id not in linked_ids:
                    linked_ids.append(candidate_id)
        # Explicit statistic assignments require an evaluated value even when
        # their spelling contains no character recognized by the numeric lexer.
        for slot in slots:
            if slot.has_evaluation(evaluations):
                continue
            left, right = slot.value_span
            if value_span is not None and not (value_span[0] <= left <= right <= value_span[1]):
                continue
            if left == right:
                left, right = slot.statement_span
            refs.append(
                {
                    "token": statement[left:right],
                    "supported": False,
                    "reason": "unparsed_statistic_value",
                    "statement_type": slot.statement_type,
                    "statistic": slot.label,
                    "validation": "unparsed",
                    "statement_span": [left, right],
                }
            )
        if refs:
            for ref in refs:
                span = ref.get("statement_span")
                if not isinstance(span, list | tuple) or len(span) != 2:
                    continue
                positions = statement_source_map[span[0] : span[1]]
                if positions and all(position is not None for position in positions):
                    a, b = positions[0], positions[-1]
                    assert a is not None and b is not None
                    ref["source_span"] = [a, b + 1]
                    ref["source_token"] = final_text[a : b + 1]
                    try:
                        SourceProjection(statement, statement_source_map).source_span(*span)
                    except ValueError:
                        ref["supported"] = False
                        ref["reason"] = "noncontiguous_quantity_source"
            refs.sort(
                key=lambda ref: reference_positions.get(id(ref), ref.get("statement_span", [-1])[0])
            )
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


def _collect_chart_values(option: JsonObject, output: list[JsonObject], args: JsonObject) -> None:
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
    context = record.summary.get("numeric_context", {})
    if not isinstance(context, dict):
        return {"statistic": "invalid", "evidence_path": path, "reason": "invalid_numeric_context"}
    for key in ("tool_args", "encoding"):
        nested = context.get(key, {})
        if not isinstance(nested, dict):
            return {
                "statistic": "invalid",
                "evidence_path": path,
                "reason": "invalid_numeric_context",
            }
        context = {**context, **nested}
    projection = record.summary.get("fact_projection")
    if isinstance(projection, dict) and valid_count_projection(
        record.summary,
        tool=str(record.source.get("tool")),
        run_id=record.run_id,
        invocation_id=record.invocation_id,
        result_hash=record.result_hash,
    ):
        context = {**projection["arguments"], "data_version_hash": projection["branch_hash"]}
    item: JsonObject = next(
        (item for item in record.summary.get("value_index", []) if item.get("path") == path),
        {},
    )
    if record.source.get("tool") == "transform_dataset":
        projection = record.summary.get("fact_projection")
        if "fact_projection" in record.summary and not isinstance(projection, dict):
            return {"statistic": "invalid", "evidence_path": path}
        policy = next(
            (fact for fact in (projection or {}).get("policies", []) if fact.get("path") == path),
            None,
        )
        fact = (
            {
                "role": "retention_policy",
                "operation": policy["operation"],
                "basis": "operation_policy",
                "columns": policy["columns"],
            }
            if policy
            else (
                item.get("fact", {})
                if projection
                else {
                    "role": {"$.rows_before": "before", "$.rows_after": "after"}.get(path),
                }
            )
        )
        operations = {key: context[key] for key in OPERATIONS if key in context}
        operation = fact.get("operation") or (
            next(iter(operations)) if len(operations) == 1 else "transform"
        )
        input_ref = projection.get("input_ref") if projection else context.get("dataset_ref")
        output_ref = projection.get("output_ref") if projection else None
        step = fact.get("step")
        dataset_ref = output_ref if fact.get("role") == "after" and output_ref else input_ref
        if step is not None and not (
            (step == 0 and fact.get("role") == "before")
            or (step == (projection or {}).get("step_count", 0) - 1 and fact.get("role") == "after")
        ):
            dataset_ref = None  # Intermediate operation states are not materialized datasets.
        return {
            "dataset_ref": dataset_ref,
            "dataset_version": context.get("data_version_hash", input_ref),
            "input_ref": input_ref,
            "output_ref": output_ref,
            "metric": "retention_policy"
            if policy
            else "column_count"
            if fact.get("role") == "column_count"
            else "row_count",
            "statistic": fact.get("role"),
            "unit": "policy" if policy else "count",
            **({"policy": policy["value"]} if policy else {}),
            "operation": operation,
            "operation_parameters": operations,
            "count_basis": fact.get("basis", "rows"),
            "columns": fact.get("columns"),
            "step": step,
            "invocation_id": record.invocation_id,
            "result_hash": record.result_hash,
            "filters": context.get("filters", []),
            "grain": None,
            "group_key": None,
            "evidence_path": path,
        }
    if record.source.get("tool") == "generate_report" and isinstance(
        item.get("report_binding"), dict
    ):
        return {**item["report_binding"], "evidence_path": path}
    if record.source.get("tool") == "get_data_profile" and (fact := profile_fact(item)):
        return {
            "dataset_ref": context.get("dataset_ref"),
            "dataset_version": context.get("data_version_hash", context.get("dataset_ref")),
            "analysis": {"tool": "get_data_profile"},
            "aggregation": None,
            "filters": context.get("filters", []),
            **(
                {"population": context["profile_population"]}
                if "profile_population" in context
                else {}
            ),
            "grain": None,
            "group_key": None,
            **fact,
            "evidence_path": path,
        }
    role = item.get("statistic") or statistic_role(path)
    source_tool = record.source.get("tool")
    if (
        source_tool == "aggregate_preview"
        and role == "group_total"
        and (
            path != "$.group_total"
            or (
                "fact_projection" in record.summary
                and (
                    not isinstance(projection, dict)
                    or path not in projection.get("paths", [])
                    or item.get("fact", {}).get("role") != "group_total"
                    or not valid_count_projection(
                        record.summary,
                        tool=str(source_tool),
                        run_id=record.run_id,
                        invocation_id=record.invocation_id,
                        result_hash=record.result_hash,
                    )
                )
            )
        )
    ):
        return {"statistic": "invalid", "evidence_path": path}
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
            if role
            in {
                "rate",
                "ratio",
                "null_ratio",
                "duplicate_ratio",
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


def _source_context_matches(
    record: EvidenceRecord,
    binding: JsonObject,
    source_context: str,
    constraint_context: str,
) -> bool:
    """Apply original-parent identity/scope before any tool-specific value role.

    Parent containers do not supply a child's metric or statistic. Their
    explicit source/range constraints nevertheless apply to every child,
    including branches which return before generic statistic binding.
    """
    if not source_context:
        return True
    context = record.summary.get("numeric_context") or {}
    projection = record.summary.get("fact_projection") or {}
    columns = set(map(str, projection.get("columns", [])))
    columns.update(map(str, context.get("columns", [])))
    for item in record.summary.get("value_index", []):
        if not isinstance(item, dict):
            continue
        item_binding = _semantic_binding(record, str(item.get("path")))
        columns.update(
            str(item_binding[key]) for key in ("metric", "grain") if item_binding.get(key)
        )
    filters = binding.get("filters", [])
    columns.update(str(item["column"]) for item in filters)
    # Delimiters around an identity are syntax, not part of its opaque value.
    # Exact source field names (including unit brackets) remain identities.
    protected = [
        match.span()
        for column in columns
        for match in re.finditer(re.escape(column), source_context)
    ]
    identity_view = list(source_context)
    for node in parse_text_surface(source_context).nodes:
        if node.kind != "bracket" or not node.closed:
            continue
        if any(a <= node.span[0] < node.span[1] <= b for a, b in protected):
            continue
        for left, right in node.marker_spans:
            identity_view[left:right] = " " * (right - left)
    if any(
        match["value"] != binding.get(IDENTITY_FIELDS[match["label"]])
        for match in identity_declarations("".join(identity_view))
        if identity_literal(match["value"])
    ):
        return False
    population = stated_population(constraint_context, columns)
    if population is not None:
        actual = population_filters(binding) if binding.get("profile_fact") else filters
        if (
            not population.valid
            or actual is None
            or sorted(json.dumps(item, sort_keys=True) for item in actual)
            != sorted(json.dumps(item, sort_keys=True) for item in population.filters)
        ):
            return False
    parameters = binding.get("operation_parameters")
    if isinstance(parameters, dict) and not operation_scope_matches(
        mask_population(constraint_context, population), parameters, sorted(columns)
    ):
        return False
    return True


def _semantic_candidate_matches(
    value: tuple[str, str, Decimal, str],
    statement: str,
    match: re.Match[str],
    evidence: list[EvidenceRecord],
    dimensions: dict[str, set[str]],
    table_cells: tuple[tuple[str, str], ...] | None = None,
    value_span: tuple[int, int] | None = None,
    profile_context: str = "",
    population: PopulationRequest | None = None,
    population_statement: str | None = None,
    required_population: PopulationRequest | None = None,
    table_context: ProfileTableContext | None = None,
    source_context: str = "",
    constraint_context: str = "",
    aggregate_context: AggregateMemberContext | None = None,
    profile_prose_context: ProfileProseContext | None = None,
) -> bool:
    """对完整数字子句绑定指标和分组；不按数字位置猜测或以同值证明关系。"""
    if any(
        item.qualified and item.value_span == match.span() for item in count_expressions(statement)
    ):
        return False  # Exact counts do not prove a bound or approximate quantity.
    profile_record = next(record for record in evidence if record.evidence_id == value[0])
    source_binding = _semantic_binding(profile_record, value[1])
    if source_binding.get("statistic") == "invalid" or not _source_context_matches(
        profile_record, source_binding, source_context, constraint_context
    ):
        return False
    if aggregate_context is not None and (
        profile_record.source.get("tool") != "aggregate_preview"
        or source_binding.get("metric") != aggregate_context.metric
        or source_binding.get("aggregation") != aggregate_context.aggregation
        or (
            aggregate_context.grain is not None
            and source_binding.get("grain") != aggregate_context.grain
        )
    ):
        return False
    requested_operation = operation_quantity(statement, *match.span())
    if requested_operation is not None or profile_record.source.get("tool") == "transform_dataset":
        if source_binding.get("statistic") == "column_count":
            projection = profile_record.summary.get("fact_projection") or {}
            return (
                requested_operation is None
                and column_quantity_matches(statement, *match.span(), projection.get("columns", []))
                and operation_scope_matches(
                    statement, source_binding["operation_parameters"], projection.get("columns", [])
                )
                and identity_matches(source_binding, statement, [source_binding], table_cells)
            )
        if requested_operation is None or profile_record.source.get("tool") != "transform_dataset":
            return False
        selected_operation = _semantic_binding(profile_record, value[1])
        if selected_operation.get("statistic") != requested_operation.role:
            return False
        if requested_operation.operation not in {
            None,
            "transform",
            selected_operation.get("operation"),
        }:
            return False
        if (
            requested_operation.duplicate
            and selected_operation.get("count_basis") != "duplicate_excess"
        ):
            return False
        if requested_operation.null_rows and selected_operation.get("operation") != "drop_nulls":
            return False
        projection = profile_record.summary.get("fact_projection") or {}
        if (
            requested_operation.operation in {None, "transform"}
            and selected_operation.get("step") is not None
        ):
            return False
        if (
            requested_operation.operation in {None, "transform"}
            and requested_operation.role == "after"
            and selected_operation.get("output_ref")
            and any(
                other.source.get("tool") == "transform_dataset"
                and isinstance(other.summary.get("fact_projection"), dict)
                and valid_count_projection(
                    other.summary,
                    tool="transform_dataset",
                    run_id=other.run_id,
                    invocation_id=other.invocation_id,
                    result_hash=other.result_hash,
                )
                and other.summary["fact_projection"].get("input_ref")
                == selected_operation["output_ref"]
                for other in evidence
                if other.evidence_id != profile_record.evidence_id
            )
        ):
            return False  # A materialized descendant supersedes a generic final-state claim.
        if not operation_scope_matches(
            statement, selected_operation["operation_parameters"], projection.get("columns", [])
        ):
            return False
        return identity_matches(selected_operation, statement, [selected_operation], table_cells)
    noun = count_noun(statement, *match.span())
    if noun is not None and profile_record.source.get("tool") == "aggregate_preview":
        selected_count = _semantic_binding(profile_record, value[1])
        return (
            selected_count.get("statistic") == "group_total"
            and noun in {"分组", selected_count.get("grain")}
            and identity_matches(selected_count, statement, [selected_count], table_cells)
        )
    if profile_record.source.get("tool") == "get_data_profile" and has_column_profile(
        profile_record.summary
    ):
        profile_bindings = [
            _semantic_binding(record, str(item.get("path")))
            for record in evidence
            for item in record.summary.get("value_index", [])
            if item.get("source_kind") == "number"
        ]
        selected_profile = _semantic_binding(profile_record, value[1])
        if (
            profile_prose_context is not None
            and profile_prose_context.non_null_population
            and selected_profile.get("statistic") in {"null_count", "null_ratio", "row_count"}
        ):
            return False
        if not population_matches(selected_profile, population) or not population_matches(
            selected_profile, required_population
        ):
            return False
        profile_text = profile_context + (
            population_statement if population_statement is not None else statement
        )
        identity_valid = identity_matches(
            selected_profile, source_context + " " + profile_text, profile_bindings, table_cells
        )
        if table_context is not None:
            # A typed table relation supplies the field and statistic header,
            # never a candidate value. Preserve the original cell suffix for
            # units/qualifiers and keep identity checks on the original row.
            if (
                value_span is None
                or not value_span[0] <= match.start() < match.end() <= value_span[1]
            ):
                return False
            prefix = table_context.field + table_context.role_label + "="
            cell = statement[slice(*value_span)]
            offset = len(prefix) - value_span[0]
            return identity_valid and profile_candidate_matches(
                selected_profile,
                profile_bindings,
                prefix + cell,
                offset + match.start(),
                offset + match.end(),
                value[2],
            )
        if profile_prose_context is not None:
            from apps.orchestrator.control.profile_claims import profile_prose_candidate_matches

            return identity_valid and profile_prose_candidate_matches(
                selected_profile,
                profile_bindings,
                statement,
                match.start(),
                match.end(),
                value[2],
                profile_prose_context.field,
            )
        return identity_valid and profile_candidate_matches(
            selected_profile,
            profile_bindings,
            profile_text,
            len(profile_context) + match.start(),
            len(profile_context) + match.end(),
            value[2],
        )
    for shape in re.finditer(
        r"(?P<row_count>\d+)\s*行\s*[×x*]\s*(?P<column_count>\d+)\s*列", statement
    ):
        for dimension in ("row_count", "column_count"):
            if match.span() == shape.span(dimension):
                shape_record = next(record for record in evidence if record.evidence_id == value[0])
                binding = _semantic_binding(shape_record, value[1])
                return (
                    binding.get("group_key") is None
                    and value[1].endswith("." + dimension)
                    and identity_matches(binding, statement, [binding], table_cells)
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
    if (
        selected.get("statistic") in {"value", "total_value"}
        and selected.get("metric")
        and not explicit_field_unit_matches(str(selected["metric"]), statement[match.end() :])
    ):
        return False
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
    normalized_metric_text = unicodedata.normalize("NFKC", metric_text)
    mentioned = {
        field for field in fields if unicodedata.normalize("NFKC", field) in normalized_metric_text
    }
    mentioned |= unit_qualified_field_mentions(metric_text, fields)
    if aggregate_context is not None:
        mentioned = {aggregate_context.metric}
    # Match field display spelling locally without rewriting the value text or
    # merging distinct source fields that happen to normalize to one spelling.
    if len({unicodedata.normalize("NFKC", field) for field in mentioned}) < len(mentioned):
        return False
    # Prefer complete field names over their substrings, never a nearest-prefix guess.
    mentioned = {
        field
        for field in mentioned
        if not any(field != other and field in other for other in mentioned)
    }
    mentioned_groups = {
        (dimension, group) for _, _, dimension, group in dimension_mentions(local, dimensions)
    }
    if selected.get("statistic") == "total_value" and len(mentioned_groups) > 1:
        interval = re.search(
            r"整个期间[（(]\s*(?P<start>\d{4}-\d{2}(?:-\d{2})?)\s*至\s*"
            r"(?P<end>\d{4}-\d{2}(?:-\d{2})?)\s*[）)]",
            local,
        )
        keys = {
            str(b.get("group_key"))
            for record, b in bindings
            if record.evidence_id == profile_record.evidence_id and b.get("group_key") is not None
        }
        # An explicit whole-period interval must cover this source's exact
        # returned group domain, never a different subset with the same sum.
        if (
            interval is not None
            and keys
            and all(re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", k) for k in keys)
            and (
                interval["start"] == min(keys)
                and interval["end"] == max(keys)
                and mentioned_groups
                == {
                    (str(selected.get("grain")), interval["start"]),
                    (str(selected.get("grain")), interval["end"]),
                }
            )
        ):
            mentioned_groups = set()
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
    return (
        selected.get("unit") != "ratio"
        and role not in ROLE_LABELS
        and (
            record is None
            or (record.source.get("tool") not in STATISTICAL_TOOLS and record.kind != "stats")
        )
    )


def _evidence_values(
    evidence: list[EvidenceRecord],
) -> list[tuple[str, str, Decimal, str]]:
    indexed: list[tuple[str, str, Decimal, str]] = []
    for record in evidence:
        if "fact_projection" in record.summary and not valid_count_projection(
            record.summary,
            tool=str(record.source.get("tool")),
            run_id=record.run_id,
            invocation_id=record.invocation_id,
            result_hash=record.result_hash,
        ):
            continue
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
    return parse_numeric_display(token, statement[token_end:]).factor


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


def _measurement_labels() -> set[str]:
    return {
        label
        for role, aliases in {**_PATH_ALIASES, **ROLE_LABELS}.items()
        for label in (role, *aliases)
    } | {
        "最小",
        "最大",
        "空值数",
        "缺失值数",
        "总计",
        "合计",
        "数值",
        "比例",
        "精确空值数",
        "非空数",
        "缺失比例",
    }


def _split_statements(text: str) -> list[str]:
    # Limitation extraction and repair use the same complete assertion ranges.
    return split_claim_statements(text, _measurement_labels())


def _numeric_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _NUMBER_PATTERN.finditer(text)]


def _decimal_from_value(value: int | float | Decimal) -> Decimal | None:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        normalized = Decimal(str(value))
    except InvalidOperation:
        return None
    return normalized if normalized.is_finite() else None
