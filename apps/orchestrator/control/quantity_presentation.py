"""闭合的数量呈现草稿：模型选择引用，Host 绑定完整事实与建议。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from packages.orchestration.contracts import QUANTITY_CONTRACT_VERSION
from packages.orchestration.quality_facts import content_hash, valid_quality_projection
from packages.session.models import Artifact
from packages.session.task_models import ClaimDraft, EvidenceRecord, ToolInvocation

from apps.orchestrator.control.quantity_surface import INSPECTIONS, analyze_surface, source_targets

DRAFT_SCHEMA = "quantity-presentation-draft-v1"


def is_draft_candidate(text: str) -> bool:
    """JSON 对象按词法边界归协议；转义键、未知版本或包装不能切到 raw。"""
    # Ownership precedes key decoding. Do not search for literal schema/key
    # spellings: JSON permits their characters to be Unicode escapes.
    return bool(
        re.search(r'\{\s*(?:"|\})', text) or text.lstrip().startswith("{") or DRAFT_SCHEMA in text
    )


@dataclass(frozen=True)
class PreparedCandidate:
    """保留原始候选；协议失败没有旧文本兜底分支。"""

    source_text: str
    text: str
    valid: bool
    issues: tuple[str, ...] = ()
    source_refs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CandidateValidation:
    """事实、结构和交付义务的可审计判定，不是模型自报可信度。"""

    prepared: PreparedCandidate
    claims: tuple[ClaimDraft, ...]
    quantities: tuple[dict[str, Any], ...]
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.prepared.valid and not self.issues

    @property
    def text(self) -> str:
        return self.prepared.text


def _fact_sentence(binding: dict[str, Any], value: str) -> str | None:
    role = binding.get("statistic")
    if binding.get("profile_fact"):
        field = (binding.get("object") or {}).get("column")
        labels = {
            "min": "最小值",
            "max": "最大值",
            "mean": "均值",
            "median": "中位数",
            "std": "标准差",
            "distinct_count": "不同值数量",
            "null_count": "空值数",
            "non_null_count": "非空数",
            "null_ratio": "缺失比例",
            "row_count": "行数",
            "column_count": "列数",
            "duplicate_rows": "整行重复多余记录数",
        }
        if role in labels:
            return f"{field or ''}{labels[role]}为{value}。"
    operations = {
        "filters": "筛选",
        "drop_nulls": "清洗空值",
        "drop_duplicates": "去重",
        "sort": "排序",
        "transform": "处理",
        "exclude_row_indices": "排除行",
    }
    if role in {"before", "after"} and binding.get("operation") in operations:
        return f"{operations[binding['operation']]}{'前' if role == 'before' else '后'}{value}行。"
    if role == "quality_issue_count":
        return f"工具列出{value}项规则诊断。"
    if role == "total_value" and binding.get("aggregation") == "sum" and binding.get("metric"):
        return f"{binding['metric']}总计为{value}。"
    return None


def reference_catalog(evidence: list[EvidenceRecord]) -> dict[str, dict[str, Any]]:
    """从单一 Evidence 构造只读引用视图；不接受模型提供值/角色/版本。"""
    from apps.orchestrator.control.claims import _semantic_binding
    from apps.orchestrator.control.quantity_surface import _quality_binding

    result: dict[str, dict[str, Any]] = {}
    for record in evidence:
        for item in record.summary.get("value_index", []):
            if not isinstance(item, dict) or item.get("source_kind") != "number":
                continue
            path = str(item.get("path", ""))
            binding = _semantic_binding(record, path)
            if path == "$.quality.summary.issue_count":
                if not valid_quality_projection(record.summary):
                    continue
                binding = _quality_binding(record)
            sentence = _fact_sentence(binding, str(item.get("value")))
            if not sentence or not binding.get("dataset_ref") or not binding.get("dataset_version"):
                continue
            identity = {
                "evidence_id": record.evidence_id,
                "source_hash": record.result_hash,
                "path": path,
                "binding": binding,
            }
            ref = "fact:" + content_hash(identity)
            result[ref] = {"kind": "fact", "text": sentence, **identity}
        if valid_quality_projection(record.summary):
            for advice in record.summary["quality_projection"]["recommendations"]:
                identity = {
                    "evidence_id": record.evidence_id,
                    "source_hash": record.result_hash,
                    "context": record.summary["numeric_context"],
                    "issue_id": advice["issue_id"],
                    "action": advice["action"],
                }
                result["advice:" + content_hash(identity)] = {
                    "kind": "advice",
                    "text": advice["message"],
                    "scope_binding": _quality_binding(record),
                    "columns": record.summary["quality_projection"]["columns"],
                    **identity,
                }
    for target in source_targets(evidence):
        for target_kind in {"schema", "version", "root"}:
            if target_kind == "root" and target.root is None:
                continue
            result[f"target:{target_kind}:{target.ref}"] = {
                "kind": "target",
                "target_kind": target_kind,
                "dataset_ref": target.dataset_ref,
                "version": target.version,
                "root": target.root,
                "source_hash": target.source_hash,
                "columns": target.columns,
                "scope_binding": target.scope_binding,
            }
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate_draft_key")
        obj[key] = value
    return obj


def parse_draft(
    text: str,
    evidence: list[EvidenceRecord],
    *,
    goal: str = "",
    selected_sources: list[dict[str, Any]] | None = None,
) -> str:
    """严格闭合 JSON schema，未知键/重复键/引用失效直接失败。"""
    draft = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(draft, dict) or set(draft) != {"schema", "blocks"}:
        raise ValueError("invalid_draft_envelope")
    if draft["schema"] != DRAFT_SCHEMA or not isinstance(draft["blocks"], list):
        raise ValueError("unknown_draft_schema")
    catalog = reference_catalog(evidence)
    seen = 0

    def select(ref: str) -> dict[str, Any]:
        from apps.orchestrator.control.quantity_surface import source_binding_matches_goal

        entry = catalog.get(ref, {})
        binding = entry.get("binding") or entry.get("scope_binding") or {}
        columns = set(entry.get("columns", ()))
        if not columns:
            columns = {str((binding.get("object") or {}).get("column", ""))} - {""}
        if not entry or not source_binding_matches_goal(binding, columns, goal):
            raise ValueError("source_scope_mismatch")
        if selected_sources is not None:
            selected_sources.append({"ref": ref, **entry})
        return entry

    def render(node: Any, depth: int = 0) -> str:
        nonlocal seen
        seen += 1
        if seen > 128 or depth > 8 or not isinstance(node, dict):
            raise ValueError("draft_structure_limit")
        kind = node.get("kind")
        fields = {
            "fact": {"kind", "fact_ref", "display_variant"},
            "advice": {"kind", "advice_ref"},
            "inspection": {"kind", "action", "target_ref"},
            "list": {"kind", "items", "heading_style"},
            "prose": {"kind", "text"},
        }
        if not isinstance(kind, str) or kind not in fields or set(node) != fields[kind]:
            raise ValueError("unknown_or_authority_bearing_draft_field")
        if kind in {"fact", "advice"}:
            key = node[kind + "_ref"]
            if not isinstance(key, str) or key not in catalog or catalog[key]["kind"] != kind:
                raise ValueError("unbound_source_reference")
            if kind == "fact" and node["display_variant"] != "statement":
                raise ValueError("unsupported_fact_display")
            return str(select(key)["text"])
        if kind == "inspection":
            action, ref = node["action"], node["target_ref"]
            if not isinstance(action, str) or action not in INSPECTIONS or not isinstance(ref, str):
                raise ValueError("unknown_inspection")
            target = select(ref)
            if (
                target.get("kind") != "target"
                or target.get("target_kind") != INSPECTIONS[action][2]
            ):
                raise ValueError("wrong_inspection_target")
            return INSPECTIONS[action][1]
        if kind == "prose":
            if not isinstance(node["text"], str):
                raise ValueError("invalid_prose")
            return node["text"]
        if not isinstance(node["items"], list) or not node["items"]:
            raise ValueError("unfulfilled_list")
        if node["heading_style"] not in {"advice", "members", "none"}:
            raise ValueError("unknown_heading_style")
        items = [render(item, depth + 1) for item in node["items"]]
        title = (
            f"本回答的建议列表包含{len(items)}个列表项：\n"
            if node["heading_style"] != "none"
            else ""
        )
        return title + "\n".join("- " + item.replace("\n", "\n  ") for item in items)

    rendered = "\n\n".join(render(block) for block in draft["blocks"])
    if not rendered.strip():
        raise ValueError("empty_draft_delivery")
    return rendered


def prepare_candidate(
    text: str,
    evidence: list[EvidenceRecord],
    *,
    goal: str = "",
    run_id: str | None = None,
    invocations: list[ToolInvocation] | None = None,
    artifacts: list[Artifact] | None = None,
    draft_required: bool = False,
) -> PreparedCandidate:
    """准备可见候选并重验全部声明；无效封套不能当普通文本再试一次。"""
    from apps.orchestrator.control.claims import extract_numeric_claims

    explicit = is_draft_candidate(text)
    rendered = text
    selected: list[dict[str, Any]] = []
    if explicit or draft_required:
        try:
            rendered = parse_draft(text, evidence, goal=goal, selected_sources=selected)
        except (ValueError, TypeError, RecursionError) as error:
            return PreparedCandidate(text, text, False, ("invalid_draft:" + str(error),))
    claims = extract_numeric_claims(
        final_text=rendered,
        goal=goal,
        evidence=evidence,
        run_id=run_id,
        invocations=invocations,
        artifacts=artifacts,
    )
    _, audit_issues = quantity_audit(rendered, evidence, claims, goal=goal)
    issues = tuple(
        dict.fromkeys(
            [
                str(ref.get("reason", "unsupported_quantity"))
                for claim in claims
                for ref in claim.value_refs
                if ref.get("supported") is not True
            ]
            + list(audit_issues)
        )
    )
    return PreparedCandidate(text, rendered, not issues, issues, tuple(selected))


def validate_candidate(
    text: str,
    evidence: list[EvidenceRecord],
    *,
    goal: str = "",
    run_id: str | None = None,
    invocations: list[ToolInvocation] | None = None,
    artifacts: list[Artifact] | None = None,
    draft_required: bool = False,
) -> CandidateValidation:
    """提供四门审计接口；完整 Verifier 仍须独立核验 TaskContract。"""
    from apps.orchestrator.control.claims import extract_numeric_claims

    prepared = prepare_candidate(
        text,
        evidence,
        goal=goal,
        run_id=run_id,
        invocations=invocations,
        artifacts=artifacts,
        draft_required=draft_required,
    )
    claims = extract_numeric_claims(
        final_text=prepared.text,
        goal=goal,
        evidence=evidence,
        run_id=run_id,
        invocations=invocations,
        artifacts=artifacts,
    )

    quantities, audit_issues = quantity_audit(
        prepared.text,
        evidence,
        claims,
        goal=goal,
        source_mode="host_rendered" if prepared.source_refs else "raw_text",
    )
    issues = tuple(dict.fromkeys((*prepared.issues, *audit_issues)))
    return CandidateValidation(prepared, tuple(claims), quantities, issues)


def quantity_audit(
    text: str,
    evidence: list[EvidenceRecord],
    claims: list[ClaimDraft],
    *,
    goal: str,
    source_mode: str = "raw_text",
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """按原文跨度合流结构与真实绑定；不以同值数字推定数量所有者。"""
    from apps.orchestrator.control.claims import _extract_numeric_claims

    def supported_fact(member: str) -> bool:
        refs = [
            r
            for c in _extract_numeric_claims(final_text=member, goal=goal, evidence=evidence)
            for r in c.value_refs
        ]
        return bool(refs) and all(r.get("supported") is True for r in refs)

    surface = analyze_surface(text, evidence, fact_validator=supported_fact, goal=goal)
    by_span = {tuple(q["source_span"]): dict(q) for q in surface.quantities}
    audit_issues = list(surface.issues)
    for claim in claims:
        for ref in claim.value_refs:
            if "token" not in ref or str(ref["token"]).startswith("["):
                continue
            span = ref.get("source_span")
            if span is None and claim.statement == text:
                span = ref.get("statement_span")
            if not isinstance(span, list | tuple) or len(span) != 2:
                audit_issues.append("unmapped_quantity_source")
                continue
            left, right = span
            if not (0 <= left < right <= len(text)):
                audit_issues.append("invalid_quantity_source_span")
                continue
            audit = by_span.setdefault(
                tuple(span),
                {
                    "token": text[left:right],
                    "source_span": list(span),
                    "quantity_purpose": "unknown",
                    "owner": "unbound",
                    "binding": "unknown",
                    "structure": "complete" if surface.complete else "incomplete",
                    "obligation": "blocked",
                },
            )
            if audit.get("quantity_purpose") == "document":
                continue  # A business value can never lend authority to document membership.
            if audit.get("quantity_purpose") == "pending_outline":
                continue
            supported = ref.get("supported") is True
            binding = ref.get("semantic_binding")
            if ref.get("kind") == "advice_parameter":
                audit.update(
                    owner="instruction_parameter",
                    binding="supported",
                    obligation="fulfilled",
                    advice_proof=ref.get("advice_proof"),
                )
            else:
                audit.update(
                    quantity_purpose="business" if binding else audit["quantity_purpose"],
                    binding="supported" if supported else "unsupported",
                    obligation="fulfilled" if supported else "blocked",
                    evidence_id=ref.get("evidence_id"),
                    evidence_path=ref.get("path"),
                    semantic_binding=binding,
                )
            audit["source_hash"] = surface.source_hash
            audit["source_mode"] = source_mode
    for audit in by_span.values():
        if audit.get("obligation") in {"blocked", "pending"}:
            audit_issues.append("unresolved_quantity_obligation")
    return tuple(by_span.values()), tuple(dict.fromkeys(audit_issues))


def generation_context(evidence: list[EvidenceRecord]) -> str:
    """只给同一 Agent 合法引用目录与封闭协议，不提供测试 oracle 或新请求。"""
    catalog = reference_catalog(evidence)
    if not catalog:
        return ""
    visible = [{"ref": ref, **entry} for ref, entry in catalog.items()]
    return (
        "数量呈现契约="
        + QUANTITY_CONTRACT_VERSION
        + "。事实必须绑定目录来源；不能自行更换角色或数值。"
        "可在最终content选择闭合JSON草稿：{schema:'"
        + DRAFT_SCHEMA
        + "',blocks:[...]}（使用合法JSON双引号）。"
        "Block只允许fact(kind,fact_ref,display_variant='statement')、advice(kind,advice_ref)、"
        "inspection(kind,action,target_ref)、list(kind,items,heading_style='advice'|'members'|'none')、"
        "prose(kind,text)。不能写value/role/unit/supported等字段。自由文字仍需同一事实/结构核验。"
        "检查动作="
        + json.dumps(list(INSPECTIONS), ensure_ascii=False)
        + "。目录="
        + json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    )
