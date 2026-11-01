"""Frozen extra negative: a correct required total cannot hide a wrong field."""

import json
from dataclasses import asdict

from apps.orchestrator.control.claims import extract_claims
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord, ToolInvocation

from test_r3_report_producer_holdout import report_witness  # noqa: F401


def test_correct_sales_total_cannot_cover_unsupported_profit_total(report_witness, tmp_path):  # noqa: F811
    witness = report_witness
    evidence = [EvidenceRecord(**witness[key]) for key in ("analysis_evidence", "report_evidence")]
    report, source = Artifact(**witness["report"]), Artifact(**witness["source"])
    invocations = [ToolInvocation(**value) for value in witness["invocations"]]
    contract = build_minimal_contract(
        run_id=witness["run"]["run_id"],
        user_text="生成报告，同时回答销售额总计。",
        columns=("销售额",),
        dataset_refs=(source.dataset_ref,),
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    answer = "销售额总计270。利润总计270。报告已生成。"
    claims = extract_claims(final_text=answer, goal="", evidence=evidence)
    verdict = verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[report, source],
        invocations=invocations,
        evidence=evidence,
        claims=claims,
        source_artifacts=[source],
    )
    result = {
        "input": answer,
        "independent_oracle": witness["independent_oracle"],
        "claims": [asdict(claim) for claim in claims],
        "verdict": asdict(verdict),
        "source_layer": witness["layer"],
    }
    (tmp_path / "mixed-witness.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert not verdict.passed, result
    wrong = [ref for claim in claims if "利润" in claim.statement for ref in claim.value_refs]
    assert wrong and any(ref.get("supported") is False for ref in wrong), result
