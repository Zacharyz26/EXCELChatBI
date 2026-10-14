"""报告须继承来源调用版本；不能把旧版本或其他数据集总计合并。"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.artifacts import artifact_payload_for
from apps.orchestrator.control.claims import (
    _semantic_binding,
    build_evidence_summary,
    extract_claims,
)
from apps.orchestrator.control.report_delivery import verified_report_totals
from apps.orchestrator.control.verifier import verify_completion
from packages.common.config import get_settings
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import ArtifactDraft

from tests.test_agent_tools import _registry
from tests.test_task_store import _workspace

D = "d" * 32
ARGS = {"dataset_ref": D, "group_col": "月份", "value_col": "销售额", "agg": "sum"}
PAYLOAD = {
    "group_col": "月份",
    "value_col": "销售额",
    "agg": "sum",
    "total_value": 270,
    "group_total": 2,
    "truncated": False,
    "rows": [{"group": "一月", "value": 120}, {"group": "二月", "value": 150}],
}


def source_analysis(tmp_path: Path, *, supplied_version=None, grow_scope=False):
    session, tasks, project, conversation, message = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="version-proof",
        user_text="生成报告，同时回答销售额总计。",
        columns=("销售额",),
        dataset_refs=(D,),
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project,
        conversation_id=conversation,
        user_message_id=message,
        contract=contract,
        budget={"max_tool_calls": 3},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="analysis",
        tool_name="aggregate_preview",
        arguments=ARGS,
        idempotency_key="analysis",
        policy_decision={"allowed": True},
    )
    reserved_version = tasks.data_version_hash(run.run_id)
    if grow_scope:
        # Simulate a sibling completion extending the visible binding set after
        # reservation; the already reserved analysis must retain its own snapshot.
        session.register_dataset(
            ref="e" * 32, project_id=project, filename="sibling.xlsx", profile={"columns": []}
        )
        with tasks._connection() as connection, connection:
            connection.execute(
                "INSERT INTO task_dataset_bindings "
                "(run_id,dataset_ref,binding_kind,parent_ref,producing_invocation_id,"
                "dataset_created_at,bound_at) VALUES (?,?,'initial',NULL,NULL,'now','now')",
                (run.run_id, "e" * 32),
            )
        assert tasks.data_version_hash(run.run_id) != reserved_version
    assistant = session.append_message(
        conversation_id=conversation, role="assistant", content="分析"
    )
    metadata = {} if supplied_version is None else {"data_version_hash": supplied_version}
    summary = build_evidence_summary(
        summary="", result=PAYLOAD, artifact_id=None, arguments={**ARGS, **metadata}
    )
    draft = ArtifactDraft(
        type="table",
        payload=copy.deepcopy(PAYLOAD),
        file_ref=None,
        source_tool="aggregate_preview",
        params={**ARGS, "analysis_id": "monthly", **metadata},
        dataset_ref=D,
    )
    summary_before, params_before = copy.deepcopy(summary), copy.deepcopy(draft.params)
    run, invocation, record, artifact, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result=PAYLOAD,
        evidence_kind="tool_result",
        evidence_source={"tool": "aggregate_preview"},
        evidence_summary=summary,
        artifact_draft=draft,
    )
    assert summary == summary_before and draft.params == params_before
    return session, tasks, run, contract, assistant, invocation, record, artifact, reserved_version


@pytest.mark.parametrize("supplied_version", [None, "forged-version"])
@pytest.mark.parametrize("grow_scope", [False, True])
def test_atomic_projections_keep_reserved_version(tmp_path, supplied_version, grow_scope):
    session, tasks, run, _, _, _, record, artifact, version = source_analysis(
        tmp_path, supplied_version=supplied_version, grow_scope=grow_scope
    )
    assert artifact.params["data_version_hash"] == version
    assert record.summary["numeric_context"]["data_version_hash"] == version
    assert _semantic_binding(record, "$.total_value")["dataset_version"] == version
    assert tasks.list_evidence_ledger(run.run_id)[0].data_version_hash == version
    assert session.list_artifacts(run.conversation_id)[0].params["data_version_hash"] == version
    assert tasks.list_evidence(run.run_id)[0].summary == record.summary


@pytest.mark.parametrize(
    "mutation",
    ["valid", "old_version", "other_dataset", "changed_source_version", "changed_proof_version"],
)
def test_report_and_analysis_only_share_matching_provenance(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    session, tasks, run, contract, assistant, analysis_call, analysis, source, version = (
        source_analysis(tmp_path)
    )
    args = {"analysis_ids": ["monthly"], "title": "分析报告", "include_pdf": False}
    result = _registry(AgentContext(session, run.project_id, run.conversation_id)).execute(
        "generate_report", json.dumps(args)
    )
    run, call, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="report",
        tool_name="generate_report",
        arguments=args,
        idempotency_key="report",
        policy_decision={"allowed": True},
    )
    run, call, report_record, report, _, _ = tasks.commit_tool_success(
        call.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result=result,
        evidence_kind="tool_result",
        evidence_source={"tool": "generate_report"},
        evidence_summary=build_evidence_summary(
            summary="", result=result, artifact_id=None, arguments=args
        ),
        artifact_draft=ArtifactDraft(
            type="report",
            payload=artifact_payload_for("generate_report", result),
            file_ref=result["md_path"],
            source_tool="generate_report",
            params=args,
            dataset_ref=None,
        ),
    )
    binding = report.payload["validation"]["total_deliveries"][0]["semantic_binding"]
    assert binding["dataset_version"] == version
    assert binding["dataset_ref"] == D
    if mutation == "old_version":
        analysis.summary["numeric_context"]["data_version_hash"] = "old-version"
    if mutation == "other_dataset":
        analysis.summary["numeric_context"]["dataset_ref"] = "e" * 32
    if mutation == "changed_source_version":
        source.params["data_version_hash"] = "old-version"
    if mutation == "changed_proof_version":
        report.payload["validation"]["total_deliveries"][0]["semantic_binding"][
            "dataset_version"
        ] = "old-version"
    evidence = [analysis, report_record]
    answer = "销售额总计270。报告已生成。"
    claims = extract_claims(final_text=answer, goal="", evidence=evidence)
    verdict = verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[report, source],
        invocations=[analysis_call, call],
        evidence=evidence,
        claims=claims,
        source_artifacts=[source],
    )
    if mutation in {"valid", "changed_source_version", "changed_proof_version"}:
        # Re-verification must detect changes to either provenance projection,
        # even if the independent original analysis still proves the chat total.
        assert bool(verified_report_totals(report, call, report_record, [source])) is (
            mutation == "valid"
        )
    if mutation in {"valid", "old_version", "other_dataset"}:
        assert verdict.passed is (mutation == "valid"), verdict
    wrong_metric = extract_claims(final_text="利润总计270。", goal="", evidence=evidence)
    assert not all(ref["supported"] for c in wrong_metric for ref in c.value_refs)
    stale = replace(analysis, run_id="old-run")
    assert not verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[source],
        invocations=[analysis_call],
        evidence=[stale],
        claims=extract_claims(final_text=answer, goal="", evidence=[stale]),
        source_artifacts=[source],
    ).passed
    get_settings.cache_clear()
