"""Independent report-producer migration holdout; offline real report + SQLite.

Upstream monthly table is the existing source_analysis fixture (not a real
aggregate invocation); generate_report and TaskStore commits are actual code.
Independent oracle is PAYLOAD rows120+150=270, metric销售额, never利润.
"""

import asyncio
import json
import socket
from dataclasses import asdict

import pytest
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.artifacts import artifact_payload_for
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.report_delivery import validation_hash, verified_report_totals
from apps.orchestrator.control.verifier import verify_completion
from packages.common.config import get_settings
from packages.orchestration.contracts import REPORT_QUANTITY_VALIDATOR
from packages.session.models import ArtifactDraft

from tests.test_agent_tools import _registry
from tests.test_batch2_report_versions import PAYLOAD, source_analysis


@pytest.fixture
def report_witness(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Independent report producer tests prohibit network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    assert sum(row["value"] for row in PAYLOAD["rows"]) == 270
    session, tasks, run, contract, assistant, analysis_call, analysis, source, version = (
        source_analysis(tmp_path)
    )
    args = {"analysis_ids": ["monthly"], "title": "分析报告", "include_pdf": False}
    registry = _registry(
        AgentContext(session, run.project_id, run.conversation_id, run_id=run.run_id)
    )
    result = registry.execute("generate_report", json.dumps(args))
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
    statements = {"correct": "销售额总计270。报告已生成。", "wrong_field": "利润总计270。"}
    evidence = [analysis, report_record]
    evaluations = {}
    for key, answer in statements.items():
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
        evaluations[key] = {
            "answer": answer,
            "claims": [asdict(claim) for claim in claims],
            "verdict": asdict(verdict),
        }
    witness = {
        "layer": "real generate_report and SQLite; synthetic upstream monthly artifact; no model",
        "independent_oracle": {"metric": "销售额", "total": 270, "rows": [120, 150]},
        "raw_report_result": result,
        "report": asdict(report),
        "report_evidence": asdict(report_record),
        "analysis_evidence": asdict(analysis),
        "source": asdict(source),
        "run": asdict(run),
        "contract": contract.to_dict(),
        "source_version": version,
        "invocations": [asdict(analysis_call), asdict(call)],
        "evaluations": evaluations,
        "verified_report_totals": verified_report_totals(report, call, report_record, [source]),
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    try:
        yield witness
    finally:
        asyncio.run(registry.aclose())
        get_settings.cache_clear()


def test_report_total_retains_typed_evidence_and_complete_verifier(report_witness):
    witness = report_witness
    validation = witness["report"]["payload"]["validation"]
    summary = witness["report_evidence"]["summary"]
    assert validation["validator"] == REPORT_QUANTITY_VALIDATOR, witness
    assert summary.get("report_validation_hash") == validation_hash(validation), witness
    indexed = [item for item in summary["value_index"] if item.get("report_binding")]
    assert indexed and all(
        item["report_binding"]["metric"] == "销售额" for item in indexed
    ), witness
    assert witness["verified_report_totals"], witness
    assert witness["evaluations"]["correct"]["verdict"]["verdict"] == "PASS", witness


def test_wrong_metric_has_a_real_rejected_claim_not_empty_refs(report_witness):
    witness = report_witness
    evaluation = witness["evaluations"]["wrong_field"]
    refs = [ref for claim in evaluation["claims"] for ref in claim["value_refs"]]
    assert refs, witness
    numerical = [ref for ref in refs if ref.get("token") == "270"]
    assert numerical and any(ref.get("supported") is False for ref in numerical), witness
    assert evaluation["verdict"]["verdict"] != "PASS", witness
