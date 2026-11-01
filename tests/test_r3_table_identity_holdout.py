"""R3 table adaptation must retain explicit dataset/version/scope owners."""

import json
from dataclasses import asdict, replace

import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

from tests.test_r3_independent_surface import FIELD, source  # noqa: F401


@pytest.mark.parametrize(
    "mutation",
    ["correct", "wrong-dataset", "wrong-version", "swapped-identity-columns", "wrong-scope"],
)
def test_profile_table_cannot_drop_identity_columns_or_preceding_scope(source, tmp_path, mutation):  # noqa: F811
    record, artifact = source
    dataset = artifact.dataset_ref
    version = "a" * 64
    args = {**artifact.params, "data_version_hash": version}
    artifact = replace(artifact, params=args)
    record = replace(
        record,
        summary=build_evidence_summary(
            summary="frozen synthetic identity",
            result=artifact.payload,
            artifact_id=None,
            arguments=args,
        ),
    )
    d, v = dataset, version
    if mutation == "wrong-dataset":
        d = "b" * 32
    if mutation == "wrong-version":
        v = "b" * 64
    if mutation == "swapped-identity-columns":
        d, v = version, dataset
    text = f"|字段|数据集|版本|最大值|\n|---|---|---|---|\n|{FIELD}|{d}|{v}|8|"
    if mutation == "wrong-scope":
        text = f"筛选条件：{FIELD}=4。\n" + text
    expected = mutation == "correct"
    candidate = validate_candidate(text, [record], goal="复核表格事实")
    contract = build_minimal_contract(
        run_id="surface-run",
        user_text="复核表格事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=list(candidate.claims),
    )
    try:
        report = validate_report_narratives([artifact], {"title": "表格复核", "insights": text})
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    witness = {
        "mutation": mutation,
        "text": text,
        "expected": expected,
        "source": asdict(record),
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert (report.get("status") == "passed") is expected, witness
