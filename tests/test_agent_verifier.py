"""Deterministic completion postcondition tests."""

from __future__ import annotations

from pathlib import Path

from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.verifier import verify_completion
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord, ToolInvocation


def _artifact(
    *, artifact_type: str, payload: dict[str, object], file_ref: str | None = None
) -> Artifact:
    return Artifact(
        id="artifact-1",
        conversation_id="conversation-1",
        message_id="message-1",
        type=artifact_type,
        payload=payload,
        file_ref=file_ref,
        source_tool="generate_report" if artifact_type == "report" else "gen_chart",
        params={},
        dataset_ref=None,
        created_at="now",
    )


def test_model_stopping_does_not_pass_without_required_chart() -> None:
    contract = build_minimal_contract(
        run_id="run-1",
        user_text="请生成图表",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )

    result = verify_completion(
        contract=contract,
        final_text="图表已经生成。",
        artifacts=[],
        invocations=[],
        evidence=[],
    )

    assert result.verdict == "NEEDS_ACTION"
    assert result.issues[0].code == "missing_chart_artifact"


def test_report_requires_a_real_nonempty_pdf_file(tmp_path: Path) -> None:
    contract = build_minimal_contract(
        run_id="run-2",
        user_text="导出 PDF 报告",
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )
    pdf_path = tmp_path / "report.pdf"
    report = _artifact(
        artifact_type="report",
        payload={
            "report_id": "report-1",
            "md_url": "/analyze/report/report-1.md",
            "pdf_url": "/analyze/report/report-1.pdf",
        },
        file_ref=str(pdf_path),
    )
    missing = verify_completion(
        contract=contract,
        final_text="PDF 已生成。",
        artifacts=[report],
        invocations=[],
        evidence=[],
    )
    assert missing.verdict == "NEEDS_ACTION"

    pdf_path.write_bytes(b"%PDF-1.7\n")
    passed = verify_completion(
        contract=contract,
        final_text="PDF 已生成。",
        artifacts=[report],
        invocations=[],
        evidence=[],
    )
    assert passed.verdict == "PASS"


def test_succeeded_tool_requires_evidence_and_budget_exhaustion_blocks() -> None:
    contract = build_minimal_contract(
        run_id="run-3",
        user_text="查询",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    invocation = ToolInvocation(
        invocation_id="invocation-1",
        run_id="run-3",
        step_id=None,
        tool_call_id="call-1",
        tool_name="kb_search",
        idempotency_key="key",
        args_hash="args",
        args={"query": "口径"},
        status="succeeded",
        result_hash="result",
        error_text=None,
        artifact_id=None,
        started_at="now",
        completed_at="now",
    )
    without_evidence = verify_completion(
        contract=contract,
        final_text="已找到定义。",
        artifacts=[],
        invocations=[invocation],
        evidence=[],
    )
    assert without_evidence.verdict == "NEEDS_ACTION"

    evidence = EvidenceRecord(
        evidence_id="evidence-1",
        run_id="run-3",
        invocation_id="invocation-1",
        artifact_id=None,
        kind="tool_result",
        source={"tool": "kb_search"},
        result_hash="result",
        summary={"hits": 1},
        created_at="now",
    )
    blocked = verify_completion(
        contract=contract,
        final_text="已找到定义。",
        artifacts=[],
        invocations=[invocation],
        evidence=[evidence],
        budget_exhausted=True,
    )
    assert blocked.verdict == "BLOCKED"


def test_unknown_tool_outcome_cannot_be_declared_complete() -> None:
    contract = build_minimal_contract(
        run_id="run-unknown",
        user_text="执行分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    invocation = ToolInvocation(
        invocation_id="invocation-unknown",
        run_id="run-unknown",
        step_id=None,
        tool_call_id="call-unknown",
        tool_name="generate_report",
        idempotency_key="key",
        args_hash="args",
        args={"title": "报告"},
        status="unknown",
        result_hash=None,
        error_text="连接中断，无法确认服务端是否提交",
        artifact_id=None,
        started_at="now",
        completed_at="now",
    )

    result = verify_completion(
        contract=contract,
        final_text="报告应该已经生成。",
        artifacts=[],
        invocations=[invocation],
        evidence=[],
    )

    assert result.verdict == "BLOCKED"
    assert result.issues[0].code == "unknown_tool_outcome"
