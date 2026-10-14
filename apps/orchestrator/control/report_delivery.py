"""把报告总计交付绑定到所选数据工件、实际文件和当前调用 Evidence。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from packages.common.report_safety import escape_report_markup
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


def verified_report_totals(
    report: Artifact,
    invocation: ToolInvocation,
    evidence: EvidenceRecord,
    sources: list[Artifact],
) -> list[JsonObject]:
    """重验当前报告与原来源、实际 Markdown/PDF 字节，失配时不给出总计证明。"""
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
        or evidence.summary.get("report_validation_hash") != validation_hash(validation)
        or not report.file_ref
    ):
        return []
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
        return []
    try:
        path = Path(report.file_ref)
        markdown = path.with_suffix(".md").read_text(encoding="utf-8")
        if hashlib.sha256(markdown.encode()).hexdigest() != validation.get("content_sha256"):
            return []
        if invocation.args.get("include_pdf") and (
            path.suffix != ".pdf"
            or hashlib.sha256(path.read_bytes()).hexdigest() != validation.get("pdf_sha256")
        ):
            return []
        checked = validate_report_narratives(selected, invocation.args)
        if any(validation.get(key) != value for key, value in checked.items()):
            return []
        totals = report_total_deliveries(selected, checked, markdown)
    except (OSError, UnicodeError, ValueError):
        return []
    return totals if totals == validation.get("total_deliveries") else []
