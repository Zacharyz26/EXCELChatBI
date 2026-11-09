"""把报告总计交付绑定到所选数据工件、实际文件和当前调用 Evidence。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from packages.common.report_safety import escape_report_markup
from packages.orchestration.contracts import (
    QUANTITY_CONTRACT_VERSION,
    REPORT_QUANTITY_VALIDATOR,
    QuantityContractUnavailable,
    require_quantity_contract_version,
)
from packages.orchestration.report_intent import ReportSourceIntent, ReportSourceOperand
from packages.session.models import Artifact, JsonObject
from packages.session.task_models import EvidenceRecord, ToolInvocation

from apps.orchestrator.control.claims import _semantic_binding, build_evidence_summary
from apps.orchestrator.control.report_claims import validate_report_narratives


def validation_hash(validation: JsonObject) -> str:
    """固定正文验证证明的完整内容，不把工件自身当作原始数值来源。"""
    return hashlib.sha256(
        json.dumps(validation, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def report_total_deliveries(
    artifacts: list[Artifact], validation: JsonObject, markdown: str
) -> list[JsonObject]:
    """仅为实际写入正文的确定性总计片段生成完整语义交付证明。"""
    # Reuse the actual deterministic formatters, including escaping and exact
    # number handling. A selected/previously calculated total alone is not delivery.
    from mcp_servers.report.tools import _aggregate_table_md, _stat_md

    deliveries: list[JsonObject] = []
    for artifact in artifacts:
        payload = artifact.payload or {}
        args = {**(artifact.params or {}), "dataset_ref": artifact.dataset_ref}
        if artifact.type == "table" and artifact.source_tool == "aggregate_preview":
            data = payload
            section = {**payload, "analysis_id": args.get("analysis_id", artifact.id)}
            fragment = "\n".join(_aggregate_table_md(escape_report_markup(section)))
        elif artifact.type == "stats" and artifact.source_tool == "dimension_contribution":
            data = payload.get("result", payload)
            section = {
                "kind": payload.get("kind", artifact.source_tool),
                "result": data,
                **{key: payload[key] for key in ("caption", "interpretation") if payload.get(key)},
            }
            fragment = "\n".join(_stat_md(escape_report_markup(section)))
        else:
            continue
        if (
            not artifact.dataset_ref
            or args.get("method", args.get("agg", "sum")) != "sum"
            or not args.get("value_col")
            or (artifact.params or {}).get("dataset_ref", artifact.dataset_ref)
            != artifact.dataset_ref
            or data.get("dataset_ref", artifact.dataset_ref) != artifact.dataset_ref
            or data.get("value_col", args["value_col"]) != args["value_col"]
            or data.get("agg", data.get("method", "sum")) != "sum"
            or data.get("unit", args.get("unit", "raw")) != args.get("unit", "raw")
            or fragment not in markdown
        ):
            continue
        summary = build_evidence_summary(
            summary="", result=data, artifact_id=artifact.id, arguments=args
        )
        if not any(
            item.get("path") == "$.total_value" and item.get("source_kind") == "number"
            for item in summary["value_index"]
        ):
            continue
        record = EvidenceRecord(
            artifact.id,
            "report-source",
            "",
            artifact.id,
            artifact.type,
            {"tool": artifact.source_tool},
            validation["source_hashes"][artifact.id],
            summary,
            artifact.created_at,
        )
        deliveries.append(
            {
                "artifact_id": artifact.id,
                "source_hash": record.result_hash,
                "value": data["total_value"],
                "semantic_binding": _semantic_binding(record, "$.total_value"),
                "fragment_sha256": hashlib.sha256(fragment.encode()).hexdigest(),
            }
        )
    return deliveries


@dataclass(frozen=True, slots=True)
class _ReportMaterial:
    selected: tuple[Artifact, ...]
    validation: JsonObject
    checked: JsonObject
    markdown: str


def _verified_report_material(
    report: Artifact,
    invocation: ToolInvocation,
    evidence: EvidenceRecord,
    sources: list[Artifact],
    *,
    quantity_contract_version: str = QUANTITY_CONTRACT_VERSION,
) -> _ReportMaterial | None:
    """重验当前报告与原来源、实际 Markdown/PDF 字节，失配时不给出总计证明。"""
    try:
        require_quantity_contract_version(quantity_contract_version)
    except QuantityContractUnavailable:
        return None
    validation = (report.payload or {}).get("validation")
    if (
        invocation.tool_name != "generate_report"
        or invocation.status != "succeeded"
        or evidence.source.get("tool") != "generate_report"
        or evidence.invocation_id != invocation.invocation_id
        or evidence.run_id != invocation.run_id
        or not invocation.result_hash
        or evidence.result_hash != invocation.result_hash
        or evidence.artifact_id != report.id
        or invocation.artifact_id != report.id
        or not isinstance(validation, dict)
        or validation.get("status") != "passed"
        or validation.get("validator") != REPORT_QUANTITY_VALIDATOR
        or validation.get("quantity_contract_version") != quantity_contract_version
        or evidence.summary.get("report_validation_hash") != validation_hash(validation)
        or not report.file_ref
    ):
        return None
    wanted = invocation.args.get("analysis_ids", [])
    selected = [
        item
        for item in sources
        if item.conversation_id == report.conversation_id
        and (item.params or {}).get("analysis_id", item.id) in wanted
    ]
    if (
        not wanted
        or {(item.params or {}).get("analysis_id", item.id) for item in selected} != set(wanted)
        or {item.id for item in selected} != set(validation.get("source_hashes", {}))
    ):
        return None
    try:
        path = Path(report.file_ref)
        markdown = path.with_suffix(".md").read_text(encoding="utf-8")
        if hashlib.sha256(markdown.encode()).hexdigest() != validation.get("content_sha256"):
            return None
        if invocation.args.get("include_pdf") and (
            path.suffix != ".pdf"
            or hashlib.sha256(path.read_bytes()).hexdigest() != validation.get("pdf_sha256")
        ):
            return None
        checked = validate_report_narratives(
            selected,
            invocation.args,
            quantity_contract_version=quantity_contract_version,
            run_id=invocation.run_id,
        )
        if any(validation.get(key) != value for key, value in checked.items()):
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    return _ReportMaterial(tuple(selected), validation, checked, markdown)


def verified_report_totals(
    report: Artifact,
    invocation: ToolInvocation,
    evidence: EvidenceRecord,
    sources: list[Artifact],
    *,
    quantity_contract_version: str = QUANTITY_CONTRACT_VERSION,
) -> list[JsonObject]:
    """Only the same verified material can supply deterministic total delivery."""
    material = _verified_report_material(
        report,
        invocation,
        evidence,
        sources,
        quantity_contract_version=quantity_contract_version,
    )
    if material is None:
        return []
    totals = report_total_deliveries(list(material.selected), material.checked, material.markdown)
    return totals if totals == material.validation.get("total_deliveries") else []


def _table_fragment(artifact: Artifact) -> str:
    from mcp_servers.report.tools import _aggregate_table_md

    payload = artifact.payload or {}
    analysis_id = (artifact.params or {}).get("analysis_id", artifact.id)
    return "\n".join(
        _aggregate_table_md(escape_report_markup({**payload, "analysis_id": analysis_id}))
    )


def report_source_deliveries(
    artifacts: list[Artifact], validation: JsonObject, assembly: JsonObject, markdown: str
) -> list[JsonObject]:
    """Called before real screenshot cleanup; bind each rendered fragment to its source."""
    from mcp_servers.report.tools import _img_data_uri

    charts = iter(assembly.get("charts", []))
    deliveries: list[JsonObject] = []
    for artifact in artifacts:
        if artifact.type not in {"table", "chart"}:
            continue
        if artifact.type == "table":
            if artifact.source_tool != "aggregate_preview":
                raise ValueError("report_source_table_unsupported")
            fragment = _table_fragment(artifact)
        else:
            section = next(charts, None)
            if not isinstance(section, dict) or not isinstance(section.get("image_path"), str):
                raise ValueError("report_source_chart_not_rendered")
            fragment = f"![chart]({_img_data_uri(section['image_path'])})"
        if not fragment or fragment not in markdown:
            raise ValueError("report_source_fragment_not_delivered")
        deliveries.append(
            {
                "artifact_id": artifact.id,
                "source_hash": validation["source_hashes"][artifact.id],
                "type": artifact.type,
                "fragment_sha256": hashlib.sha256(fragment.encode()).hexdigest(),
            }
        )
    if next(charts, None) is not None:
        raise ValueError("report_source_chart_mapping_invalid")
    return deliveries


def _matches_source_operand(operand: ReportSourceOperand, artifact: Artifact) -> bool:
    from packages.orchestration.report_intent import source_operand_matches

    return source_operand_matches(
        operand,
        artifact_type=artifact.type,
        source_tool=artifact.source_tool,
        params=artifact.params or {},
        payload=artifact.payload or {},
    )


def report_sources_match(intent: ReportSourceIntent, sources: list[Artifact]) -> bool:
    """Every complete source operand needs a matching selected material."""
    return all(
        any(_matches_source_operand(operand, source) for source in sources)
        for operand in intent.required_operands
    )


def verified_report_sources(
    report: Artifact,
    invocation: ToolInvocation,
    evidence: EvidenceRecord,
    sources: list[Artifact],
    *,
    intent: ReportSourceIntent,
    quantity_contract_version: str = QUANTITY_CONTRACT_VERSION,
) -> bool:
    """Source IDs alone are not delivery: recheck the actual embedded material."""
    material = _verified_report_material(
        report,
        invocation,
        evidence,
        sources,
        quantity_contract_version=quantity_contract_version,
    )
    if material is None:
        return False
    proofs = material.validation.get("source_deliveries")
    expected = [a for a in material.selected if a.type in {"table", "chart"}]
    if (
        not isinstance(proofs, list)
        or len(proofs) != len(expected)
        or any(not isinstance(item, dict) for item in proofs)
        or {item.get("artifact_id") for item in proofs} != {a.id for a in expected}
    ):
        return False
    # These PNG bytes are inside the hash-verified Markdown/PDF production,
    # not optional filesystem thumbnails or an earlier chart artifact ID.
    images = re.findall(r"!\[chart\]\(data:image/png;base64,([A-Za-z0-9+/=]+)\)", material.markdown)
    image_fragments: set[str] = set()
    for encoded in images:
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return False
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        fragment = f"![chart](data:image/png;base64,{encoded})"
        image_fragments.add(hashlib.sha256(fragment.encode()).hexdigest())
    for artifact in expected:
        proof = next(item for item in proofs if item.get("artifact_id") == artifact.id)
        if (
            proof.get("source_hash") != material.checked["source_hashes"].get(artifact.id)
            or proof.get("type") != artifact.type
        ):
            return False
        if artifact.type == "table":
            if artifact.source_tool != "aggregate_preview":
                return False
            fragment = _table_fragment(artifact)
            if fragment not in material.markdown or hashlib.sha256(
                fragment.encode()
            ).hexdigest() != proof.get("fragment_sha256"):
                return False
        elif proof.get("fragment_sha256") not in image_fragments:
            return False
    return report_sources_match(intent, expected)
