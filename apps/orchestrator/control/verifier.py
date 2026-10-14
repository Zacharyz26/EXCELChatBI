"""Deterministic-first completion verification for v2.4 stage 1."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from packages.common.identifiers import dataset_reference_arguments
from packages.orchestration.analysis_requirements import TOTAL_LABEL
from packages.orchestration.contracts import TaskContract
from packages.orchestration.dataset_scope import verified_analysis_scope
from packages.session.models import Artifact
from packages.session.task_models import (
    ClaimDraft,
    EvidenceRecord,
    TaskDatasetBinding,
    TaskStepRecord,
    ToolInvocation,
)

from apps.orchestrator.control.claims import extract_numeric_claims

VerificationVerdict = Literal["PASS", "NEEDS_ACTION", "WAITING_USER", "BLOCKED", "FAILED"]


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: str
    message: str
    criterion_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verdict: VerificationVerdict
    issues: tuple[VerificationIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


class SemanticCoverageChecker(Protocol):
    """Constrained checker: it may only judge coverage after hard checks pass."""

    def check(self, contract: TaskContract, final_text: str) -> VerificationResult: ...


def verify_completion(
    *,
    contract: TaskContract,
    final_text: str,
    artifacts: list[Artifact],
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    claims: list[ClaimDraft] | None = None,
    plan_steps: list[TaskStepRecord] | None = None,
    budget_exhausted: bool = False,
    semantic_checker: SemanticCoverageChecker | None = None,
    dataset_bindings: list[TaskDatasetBinding] | None = None,
    source_artifacts: list[Artifact] | None = None,
) -> VerificationResult:
    """Verify hard postconditions before allowing any semantic judgement."""
    issues: list[VerificationIssue] = []
    invocation_by_id = {
        item.invocation_id: item
        for item in invocations
        if item.run_id == contract.run_id and item.status == "succeeded"
    }
    bound_evidence = [
        replace(
            item,
            summary={
                **item.summary,
                "numeric_context": {
                    **item.summary.get("numeric_context", {}),
                    **invocation_by_id[item.invocation_id].args,
                },
            },
        )
        if item.invocation_id in invocation_by_id
        else item
        for item in evidence
    ]
    previously_supported = claims is None or all(
        ref.get("supported") is True
        for claim in claims
        if claim.claim_kind == "numeric"
        for ref in claim.value_refs
    )
    if previously_supported and any(
        ref.get("supported") is not True
        for claim in extract_numeric_claims(
            final_text=final_text, goal=contract.goal, evidence=bound_evidence
        )
        for ref in claim.value_refs
    ):
        issues.append(
            VerificationIssue(
                "numeric_semantic_mismatch", "数值无法绑定到相同指标、分组、单位和工具证据。"
            )
        )
    issues.extend(
        _verify_analysis_requirements(
            contract,
            invocations,
            evidence,
            claims or [],
            dataset_bindings or [],
            artifacts,
            source_artifacts or [],
        )
    )
    if not final_text.strip():
        issues.append(
            VerificationIssue(
                code="empty_response",
                message="模型没有返回有效的最终答复",
                criterion_id="response.non_empty",
            )
        )

    incomplete_steps = [
        step for step in plan_steps or [] if step.status not in {"completed", "skipped"}
    ]
    if incomplete_steps:
        labels = ", ".join(step.logical_id for step in incomplete_steps[:8])
        issues.append(
            VerificationIssue(
                code="incomplete_plan_steps",
                message=f"结构化计划仍有未完成步骤：{labels}",
            )
        )

    for criterion in contract.success_criteria:
        if not criterion.required or criterion.kind != "artifact":
            continue
        matching = [item for item in artifacts if item.type == criterion.artifact_type]
        if not any(_valid_artifact(item, criterion.artifact_format) for item in matching):
            issues.append(
                VerificationIssue(
                    code=f"missing_{criterion.artifact_type}_artifact",
                    message=f"缺少可验证的 {criterion.artifact_type} 工件",
                    criterion_id=criterion.criterion_id,
                )
            )
        if criterion.artifact_type == "chart" and criterion.artifact_dimensions:
            covered_dimensions = {
                dimension
                for item in matching
                if _valid_artifact(item, criterion.artifact_format)
                if (dimension := _chart_dimension(item)) is not None
            }
            for dimension in criterion.artifact_dimensions:
                if dimension in covered_dimensions:
                    continue
                issues.append(
                    VerificationIssue(
                        code="missing_chart_dimension_artifact",
                        message=f"缺少维度“{dimension}”的可验证图表工件",
                        criterion_id=criterion.criterion_id,
                    )
                )

    evidence_by_invocation = {item.invocation_id for item in evidence}
    evidence_ids = {item.evidence_id for item in evidence}
    evidence_by_id = {item.evidence_id: item for item in evidence}
    invocations_by_id = {item.invocation_id: item for item in invocations}
    artifact_ids = {item.id for item in artifacts}
    for invocation in invocations:
        if invocation.status == "unknown":
            issues.append(
                VerificationIssue(
                    code="unknown_tool_outcome",
                    message=(f"工具 {invocation.tool_name} 的执行结果无法确认，必须先按幂等键对账"),
                )
            )
            continue
        if invocation.status != "succeeded":
            continue
        if invocation.invocation_id not in evidence_by_invocation:
            issues.append(
                VerificationIssue(
                    code="missing_tool_evidence",
                    message=f"工具 {invocation.tool_name} 的成功结果没有 Evidence",
                )
            )
        if invocation.tool_name in {"gen_chart", "generate_report"} and (
            invocation.artifact_id is None or invocation.artifact_id not in artifact_ids
        ):
            artifact_name = "chart" if invocation.tool_name == "gen_chart" else "report"
            issues.append(
                VerificationIssue(
                    code=f"missing_{artifact_name}_artifact",
                    message=f"工具 {invocation.tool_name} 没有绑定真实 Artifact",
                )
            )

    last_execution_failure = _last_unrecovered_execution_failure(invocations)
    if last_execution_failure is not None:
        issues.append(
            VerificationIssue(
                code="unrecovered_tool_failure",
                message=(
                    f"工具 {last_execution_failure.tool_name} 执行失败，"
                    "且没有后续成功调用证明任务已经恢复"
                ),
            )
        )

    for claim in claims or []:
        if claim.claim_kind == "numeric":
            unsupported = [
                str(ref.get("token", "?"))
                for ref in claim.value_refs
                if ref.get("kind") != "definition_execution"
                if ref.get("supported") is not True
            ]
            if unsupported:
                issues.append(
                    VerificationIssue(
                        code="unsupported_numeric_claim",
                        message=(f"数值结论缺少当前任务 Evidence：{', '.join(unsupported)}"),
                    )
                )
        elif claim.claim_kind == "knowledge":
            unsupported_sources = [
                str(ref.get("reason", "missing_source_citation"))
                for ref in claim.value_refs
                if ref.get("kind") in {"knowledge_source", "knowledge_no_result"}
                and ref.get("supported") is not True
            ]
            if unsupported_sources:
                issues.append(
                    VerificationIssue(
                        code="unsupported_knowledge_claim",
                        message="知识结论未引用当前检索来源，或在无结果时作了肯定回答",
                    )
                )
        if _invalid_definition_lineage(claim, evidence_by_id, invocations_by_id):
            issues.append(
                VerificationIssue(
                    code="invalid_definition_lineage",
                    message=("Claim 未同时绑定领域定义 Resource 版本、定义 Evidence 与数据调用"),
                )
            )
        linked = set(claim.evidence_ids)
        referenced = {
            str(ref["evidence_id"])
            for ref in claim.value_refs
            if ref.get("supported") is True and "evidence_id" in ref
        }
        if not referenced.issubset(linked) or not linked.issubset(evidence_ids):
            issues.append(
                VerificationIssue(
                    code="invalid_claim_evidence",
                    message="Claim 的 Evidence 关联不属于当前任务或未写入关联表",
                )
            )
    if budget_exhausted and issues:
        issues.append(
            VerificationIssue(
                code="budget_exhausted",
                message="工具调用预算已耗尽，成功标准尚未全部验证",
            )
        )

    if issues:
        verdict: VerificationVerdict = (
            "BLOCKED"
            if budget_exhausted or any(item.code == "unknown_tool_outcome" for item in issues)
            else "FAILED"
            if any(item.code == "unrecovered_tool_failure" for item in issues)
            else "NEEDS_ACTION"
        )
        return VerificationResult(verdict=verdict, issues=tuple(issues))
    if semantic_checker is not None:
        return semantic_checker.check(contract, final_text)
    return VerificationResult(verdict="PASS")


def _verify_analysis_requirements(
    contract: TaskContract,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    claims: list[ClaimDraft],
    bindings: list[TaskDatasetBinding],
    artifacts: list[Artifact],
    source_artifacts: list[Artifact],
) -> list[VerificationIssue]:
    requirements = contract.analysis_requirements
    if requirements is None:
        return []
    issues: list[VerificationIssue] = []
    if requirements.filter_error:
        issues.append(
            VerificationIssue(
                "required_filter_not_satisfied", requirements.filter_error
            )
        )
    by_id = {
        item.invocation_id: item
        for item in invocations
        if item.run_id == contract.run_id
    }
    scope = verified_analysis_scope(
        requirements, contract.run_id, invocations, evidence, bindings
    )

    def violates_scope(item: ToolInvocation) -> bool:
        refs = [ref for _, ref in dataset_reference_arguments(item.args)]
        if item.run_id != contract.run_id or not any(ref not in scope for ref in refs):
            return False
        # 失败的越界尝试若没有同工具的正确补做，也不能被无关报告/画像洗成完成。
        return item.status == "succeeded" or not any(
            other.status == "succeeded"
            and other.tool_name == item.tool_name
            and other.run_id == contract.run_id
            and all(ref in scope for _, ref in dataset_reference_arguments(other.args))
            for other in invocations
        )

    if requirements.filters:
        if not scope:
            issues.append(
                VerificationIssue(
                    "required_filter_not_satisfied",
                    "用户要求的完整筛选条件尚未产生已登记的派生数据集。",
                )
            )
        if any(
            item.tool_name not in {"transform_dataset", "get_data_profile"}
            and violates_scope(item)
            for item in invocations
        ):
            issues.append(
                VerificationIssue(
                    "required_filter_not_satisfied",
                    "筛选后的分析引用了不符合本次完整条件的数据集。",
                )
            )
    elif scope and any(violates_scope(item) for item in invocations):
        issues.append(
            VerificationIssue(
                "required_dataset_not_satisfied", "分析没有使用用户绑定的数据集。"
            )
        )
    if not requirements.total:
        return issues
    total_paths: set[tuple[str, str]] = set()
    total_scopes: set[tuple[str, str]] = set()
    report_delivered = False
    if any(c.required and c.artifact_type == "report" for c in contract.success_criteria):
        from apps.orchestrator.control.report_delivery import verified_report_totals

        for record in evidence:
            invocation = by_id.get(record.invocation_id)
            report = next(
                (a for a in artifacts if a.id == record.artifact_id and a.type == "report"), None
            )
            if invocation is None or report is None or record.run_id != contract.run_id:
                continue
            for index, delivery in enumerate(
                verified_report_totals(report, invocation, record, source_artifacts)
            ):
                binding = delivery["semantic_binding"]
                if (
                    binding.get("dataset_ref") not in scope
                    or binding.get("metric") != requirements.total_column
                    or binding.get("aggregation") != "sum"
                    or binding.get("statistic") != "total_value"
                    or binding.get("unit") != "raw"
                ):
                    continue
                total_paths.add(
                    (record.evidence_id, f"$.validation.total_deliveries[{index}].value")
                )
                total_scopes.add((binding["dataset_ref"], binding["metric"]))
                report_delivered = True
    for item in evidence:
        invocation = by_id.get(item.invocation_id)
        if (
            item.run_id != contract.run_id
            or invocation is None
            or invocation.status != "succeeded"
            or invocation.tool_name
            not in {"dimension_contribution", "aggregate_preview"}
            or invocation.args.get("method", invocation.args.get("agg", "sum")) != "sum"
            or (scope and invocation.args.get("dataset_ref") not in scope)
            or (requirements.filters and not scope)
            or (
                requirements.total_column
                and invocation.args.get("value_col") != requirements.total_column
            )
        ):
            continue
        for value in item.summary.get("value_index", []):
            if (
                value.get("path") == "$.total_value"
                and value.get("source_kind") == "number"
            ):
                total_paths.add((item.evidence_id, "$.total_value"))
                total_scopes.add(
                    (
                        str(invocation.args.get("dataset_ref")),
                        str(invocation.args.get("value_col")),
                    )
                )
    if not total_paths or len(total_scopes) != 1:
        issues.append(
            VerificationIssue(
                "missing_required_total_evidence",
                "用户要求的总计缺少同一数据集和指标的确定性工具 Evidence。",
            )
        )
        return issues
    # A requested report is a delivery surface. An explicit request to answer
    # the total in chat still requires a supported final-answer Claim below.
    if report_delivered and not re.search(
        r"(?:回答|答复|回复|告诉我|\banswer\b).{0,40}(?:总计|合计|总额|总量|\btotal\b)",
        contract.goal,
        re.I,
    ):
        return issues
    for claim in claims:
        if claim.claim_kind != "numeric" or not TOTAL_LABEL.search(claim.statement):
            continue
        if re.search(
            r"删除|缺少|无法|未提供|未返回|没有|不(?:是|为)|并非", claim.statement
        ):
            continue
        for ref in claim.value_refs:
            if ref.get("supported") is not True:
                continue
            paths = [(ref.get("evidence_id"), ref.get("path"))]
            paths.extend(
                (item.get("evidence_id"), item.get("path"))
                for item in ref.get("candidate_paths", [])
            )
            if any(pair in total_paths for pair in paths):
                return issues
    issues.append(
        VerificationIssue(
            "missing_required_total_delivery",
            "最终回答未交付用户要求且由总计 Evidence 支持的数值。",
        )
    )
    return issues


def _invalid_definition_lineage(
    claim: ClaimDraft,
    evidence_by_id: dict[str, EvidenceRecord],
    invocations_by_id: dict[str, ToolInvocation],
) -> bool:
    linked = set(claim.evidence_ids)
    bound_data: dict[str, dict[str, object]] = {}
    for evidence_id in linked:
        record = evidence_by_id.get(evidence_id)
        binding = record.source.get("definition_execution") if record is not None else None
        if isinstance(binding, dict):
            bound_data[evidence_id] = binding
    refs = [ref for ref in claim.value_refs if ref.get("kind") == "definition_execution"]
    if not bound_data:
        return bool(refs)
    for data_evidence_id, binding in bound_data.items():
        data_evidence = evidence_by_id[data_evidence_id]
        invocation = invocations_by_id.get(data_evidence.invocation_id)
        if invocation is not None and (
            invocation.tool_name != binding.get("compiled_tool_name")
            or invocation.args_hash != binding.get("compiled_arguments_hash")
        ):
            return True
        definition_evidence_id = binding.get("definition_evidence_id")
        if not isinstance(definition_evidence_id, str) or definition_evidence_id not in linked:
            return True
        definition_evidence = evidence_by_id.get(definition_evidence_id)
        resource = (
            definition_evidence.source.get("definition_resource")
            if definition_evidence is not None
            else None
        )
        if not isinstance(resource, dict) or any(
            resource.get(key) != binding.get(key)
            for key in (
                "definition_id",
                "definition_version",
                "semantic_key",
                "formula_hash",
                "resource_uri",
                "source_ref",
            )
        ):
            return True
        matching_refs = [
            ref
            for ref in refs
            if ref.get("data_evidence_id") == data_evidence_id
            and ref.get("evidence_id") == definition_evidence_id
            and ref.get("supported") is True
            and all(
                ref.get(key) == binding.get(key)
                for key in (
                    "definition_id",
                    "definition_version",
                    "semantic_key",
                    "formula_hash",
                    "resource_uri",
                    "source_ref",
                )
            )
        ]
        if len(matching_refs) != 1:
            return True
    return len(refs) != len(bound_data)


def _valid_artifact(artifact: Artifact, required_format: str | None) -> bool:
    payload = artifact.payload or {}
    if artifact.type == "chart":
        option = payload.get("option")
        return isinstance(option, dict) and bool(option)
    if artifact.type != "report":
        return artifact.payload is not None or _valid_file(artifact.file_ref)
    if not payload.get("report_id") or not payload.get("md_url"):
        return False
    if required_format == "pdf":
        if not payload.get("pdf_url") or not artifact.file_ref:
            return False
        if Path(artifact.file_ref).suffix.lower() != ".pdf":
            return False
    return _valid_file(artifact.file_ref)


def _chart_dimension(artifact: Artifact) -> str | None:
    """Read the governed x-axis dimension from persisted gen_chart arguments."""
    params = artifact.params or {}
    encoding = params.get("encoding")
    dimension = encoding.get("x") if isinstance(encoding, dict) else None
    return dimension.strip() if isinstance(dimension, str) and dimension.strip() else None


def _last_unrecovered_execution_failure(
    invocations: list[ToolInvocation],
) -> ToolInvocation | None:
    """仅用相同操作、步骤和数据作用域的成功重试恢复失败。

    Policy denials and local circuit breakers are deliberate non-executions, not
    runtime failures.  Their error messages are generated by the orchestrator
    with stable prefixes and remain eligible for an honest text-only response.
    """
    for index in range(len(invocations) - 1, -1, -1):
        invocation = invocations[index]
        error = (invocation.error_text or "").lstrip()
        if invocation.status != "failed" or error.startswith(("未执行：", "熔断：")):
            continue
        if not any(_recovers(invocation, later) for later in invocations[index + 1 :]):
            return invocation
    return None


def _recovers(failed: ToolInvocation, later: ToolInvocation) -> bool:
    if (
        later.status != "succeeded"
        or later.run_id != failed.run_id
        or later.tool_name != failed.tool_name
        or later.step_id != failed.step_id
    ):
        return False
    for key in (
        "dataset_ref",
        "left_dataset_ref",
        "right_dataset_ref",
        "value_col",
        "group_col",
        "dimension_col",
        "date_col",
        "time_col",
    ):
        if key in failed.args and failed.args[key] != later.args.get(key):
            return False
    if failed.tool_name == "transform_dataset":
        # Removing an unsatisfiable filter is a different task, not a successful retry.
        filters = later.args.get("filters", [])
        if not isinstance(filters, list) or any(
            condition not in filters for condition in failed.args.get("filters", [])
        ):
            return False
    return True


def _valid_file(file_ref: str | None) -> bool:
    if not file_ref:
        return False
    try:
        path = Path(file_ref)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
