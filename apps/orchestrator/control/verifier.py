"""Deterministic-first completion verification for v2.4 stage 1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from packages.session.models import Artifact
from packages.session.task_models import ClaimDraft, EvidenceRecord, ToolInvocation

from apps.orchestrator.control.contracts import TaskContract

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
    budget_exhausted: bool = False,
    semantic_checker: SemanticCoverageChecker | None = None,
) -> VerificationResult:
    """Verify hard postconditions before allowing any semantic judgement."""
    issues: list[VerificationIssue] = []
    if not final_text.strip():
        issues.append(
            VerificationIssue(
                code="empty_response",
                message="模型没有返回有效的最终答复",
                criterion_id="response.non_empty",
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

    evidence_by_invocation = {item.invocation_id for item in evidence}
    evidence_ids = {item.evidence_id for item in evidence}
    artifact_ids = {item.id for item in artifacts}
    for invocation in invocations:
        if invocation.status == "unknown":
            issues.append(
                VerificationIssue(
                    code="unknown_tool_outcome",
                    message=(
                        f"工具 {invocation.tool_name} 的执行结果无法确认，必须先按幂等键对账"
                    ),
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

    for claim in claims or []:
        if claim.claim_kind == "numeric":
            unsupported = [
                str(ref.get("token", "?"))
                for ref in claim.value_refs
                if ref.get("supported") is not True
            ]
            if unsupported:
                issues.append(
                    VerificationIssue(
                        code="unsupported_numeric_claim",
                        message=(
                            f"数值结论缺少当前任务 Evidence：{', '.join(unsupported)}"
                        ),
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
    if budget_exhausted:
        issues.append(
            VerificationIssue(
                code="budget_exhausted",
                message="工具调用预算已耗尽，成功标准尚未全部验证",
            )
        )

    if issues:
        verdict: VerificationVerdict = (
            "BLOCKED"
            if budget_exhausted
            or any(item.code == "unknown_tool_outcome" for item in issues)
            else "NEEDS_ACTION"
        )
        return VerificationResult(verdict=verdict, issues=tuple(issues))
    if semantic_checker is not None:
        return semantic_checker.check(contract, final_text)
    return VerificationResult(verdict="PASS")


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


def _valid_file(file_ref: str | None) -> bool:
    if not file_ref:
        return False
    try:
        path = Path(file_ref)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
